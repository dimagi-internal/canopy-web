"""Moving a session between runners: transfers, transfer requests, handoffs."""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja.errors import HttpError

from apps.api.route_gates import gate
from apps.harness import initiator as who

from . import (
    access,
    handoff,
    placement,
    transfer_requests,
)
from .schemas import (
    TransferDecisionIn,
    HandoffOut,
    TransferAllIn,
    TransferAllOut,
    TransferIn,
    TransferOut,
    TransferRequestOut,
)
# The one session router and the helpers its routes share; api.py imports this
# module after defining them.
from .api import (
    _filtered_sessions,
    _session_or_404,
    router,
)

#: Published operationIds keep this app's label (apps/api/api.py `_app_label`): an
#: operationId is an MCP tool name and a `generated.ts` key.
OPERATION_LABEL = "canopy_sessions"


# ---- transfer requests: a move onto someone else's runner, awaiting their yes ----
# `/transfer-requests` is registered before `/{session_id}` (api.py's import order),
# so it is never read as a session id.

def _transfer_request_out(req, transfer=None) -> dict:
    def email(u):
        return getattr(u, "email", "") or ""

    out = {
        "id": req.id, "session_id": req.session_id,
        "session_title": req.session.title or "",
        "agent_slug": req.session.agent.slug if req.session.agent_id else "",
        "to_runner": req.to_runner.name, "to_runner_id": req.to_runner_id,
        "from_runner": req.from_runner.name if req.from_runner_id else "",
        "requested_by": email(req.requested_by), "brief": req.brief, "status": req.status,
        "decided_by": email(req.decided_by), "decided_at": req.decided_at, "note": req.note,
        "created_at": req.created_at,
        "approvers": sorted(email(u) for u in transfer_requests.approvers(req.to_runner)),
        "transfer": None,
    }
    if transfer is not None:
        binding, turn = transfer
        out["transfer"] = {
            "session_id": str(req.session_id),
            "runner": binding.runner.name if binding.runner_id else "",
            "transferred_from": binding.transferred_from.name if binding.transferred_from_id else "",
            "index_offset": binding.index_offset, "turn_id": str(turn.id) if turn else "",
        }
    return out


def _transfer_request_or_404(request: HttpRequest, request_id: uuid.UUID):
    req = transfer_requests.visible_to(request.user).filter(pk=request_id).first()
    if req is None:
        raise HttpError(404, "transfer request not found")
    return req


@router.get("/transfer-requests", response=list[TransferRequestOut],
            summary="Transfer requests waiting on you, or that you made")
@gate("self", "runner-admin")  # ones you asked for, or for boxes you administer
def list_transfer_requests(request: HttpRequest, status: str = "pending"):
    """Requests to move a session onto a runner you administer (yours to approve
    or decline), plus the ones you asked for. `status` filters (default
    `pending`; `all` for every state)."""
    qs = transfer_requests.visible_to(request.user)
    rows = list(qs[:200])
    for req in rows:
        transfer_requests._expire_if_stale(req)
    if status != "all":
        rows = [r for r in rows if r.status == status]
    return [_transfer_request_out(r) for r in rows]


@router.post("/transfer-requests/{request_id}/approve", response=TransferRequestOut,
             summary="Approve a transfer onto your runner (and carry it out)")
@gate("runner-admin", kind="conversation")  # the target box's admins, re-checked at decision
def approve_transfer_request(request: HttpRequest, request_id: uuid.UUID):
    """Approve moving the session onto your runner; the move happens now and
    `transfer` reports it LAUNCHED. Only an administrator of the target runner may
    approve, checked at this moment. 409 while a turn is still executing on the
    session — the request stays pending; stop the session and approve again."""
    req = _transfer_request_or_404(request, request_id)
    try:
        transfer = transfer_requests.approve(req=req, user=request.user,
                                    initiator=who.for_request(request, via="transfer"))
    except PermissionError as exc:
        raise HttpError(403, str(exc))
    except LookupError as exc:
        raise HttpError(404, str(exc))
    except RuntimeError as exc:
        raise HttpError(409, str(exc))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    return _transfer_request_out(req, transfer)


@router.post("/transfer-requests/{request_id}/decline", response=TransferRequestOut,
             summary="Decline a transfer onto your runner")
@gate("runner-admin", kind="conversation")
def decline_transfer_request(request: HttpRequest, request_id: uuid.UUID, payload: TransferDecisionIn):
    """Decline; the session stays where it is. `note` is shown to the requester."""
    req = _transfer_request_or_404(request, request_id)
    try:
        transfer_requests.decline(req=req, user=request.user, note=payload.note)
    except PermissionError as exc:
        raise HttpError(403, str(exc))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    return _transfer_request_out(req)


@router.post("/transfer-requests/{request_id}/cancel", response=TransferRequestOut,
             summary="Withdraw a transfer request you made")
@gate("self", kind="conversation")  # only whoever asked
def cancel_transfer_request(request: HttpRequest, request_id: uuid.UUID):
    req = _transfer_request_or_404(request, request_id)
    try:
        transfer_requests.cancel(req=req, user=request.user)
    except PermissionError as exc:
        raise HttpError(403, str(exc))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    return _transfer_request_out(req)


# ---- moving every session off one runner at once ----
# `/transfer-all` is registered before `/{session_id}` (api.py's import order), so
# it is never read as a session id.

@router.post("/transfer-all", response=TransferAllOut,
             summary="Move every open session on one runner to another")
@gate("session-acl", kind="conversation")  # each session: access.may_move, then the transfer's own rules
def transfer_all(request: HttpRequest, payload: TransferAllIn):
    """Move every open session you can act in from `from_runner` to `to_runner`,
    each with its work (see `transfer`), and say what happened to each one.

    The way to empty a box — an account out of Claude tokens, a laptop going
    away, load moving to the cloud. `dry_run` lists what would move first. Each
    session is its own transfer, so one that cannot move (a turn executing, a
    session the target cannot run) is reported `refused` with the reason and the
    rest still move. Discovery alone is `GET /api/canopy-sessions/?runner=<name>`.
    """
    source = transfer_requests.resolve_runner(payload.from_runner)
    target = transfer_requests.resolve_runner(payload.to_runner)
    if source is None:
        raise HttpError(404, f"no runner {payload.from_runner!r}")
    if target is None:
        raise HttpError(404, f"no runner {payload.to_runner!r}")
    rows = (_filtered_sessions(request, state="active")
            .filter(runner_binding__runner=source).order_by("created_at"))
    if payload.session_ids:
        rows = rows.filter(pk__in=[_uuid_or_422(x) for x in payload.session_ids])
    results = []
    for session in rows:
        key = session.runner_binding.session_key if session.runner_binding else ""
        out = {"session_id": str(session.id), "title": session.title or "", "session_key": key,
               "status": "", "error": "", "turn_id": "", "handoff": None}
        results.append(out)
        if not access.may_move(request.user, session):
            out.update(status="refused", error="you can read this session but not move it")
            continue
        if payload.dry_run:
            cli = payload.cli if payload.cli and payload.cli != session.cli else ""
            why = (placement.busy_reason(session) or placement.cli_switch_refusal(session, cli)
                   or ("" if placement._placeable_runner(session, str(target.id), cli=cli)
                       else str(placement._placement_refused(session, str(target.id), "",
                                                             cli=cli))))
            out.update(status="refused" if why else "would_move", error=why)
            continue
        try:
            req, moved = transfer_requests.request(
                session=session, runner_value=str(target.id), brief=payload.brief,
                user=request.user, initiator=who.for_request(request, via="transfer"),
                cli=payload.cli or "")
        except (LookupError, RuntimeError, FileExistsError, ValueError) as exc:
            out.update(status="refused", error=str(exc))
            continue
        if moved is None:
            out.update(status="pending", error="waiting on an administrator of the target")
            continue
        _binding, turn = moved
        h = session.handoffs.select_related("source_runner", "target_runner").first()
        out.update(status="moved", turn_id=str(turn.id) if turn else "",
                   handoff=handoff.summary(h) if h is not None else None)
    return {"from_runner": source.name, "to_runner": target.name, "results": results}


def _uuid_or_422(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise HttpError(422, f"not a session id: {value!r}")


@router.post(
    "/{session_id}/transfer", response=TransferOut,
    summary="Move a live session onto another runner",
)
@gate("session-acl", kind="conversation")  # access.may_move: its writers, or an admin of its box
def transfer(request: HttpRequest, session_id: uuid.UUID, payload: TransferIn):
    """Move a session between boxes — cloud -> laptop, or between the two macOS
    accounts — carrying its message history across.

    `place` was the closest thing before this and it is not the same operation:
    it re-pins one queued turn and leaves the binding where it was, so the next
    ship still 404s and the next send sticks to the old box. The failure that
    motivated this endpoint was doing the move by hand with `place`/`send` —
    execution DID move, and the session's entire pre-transfer history was deleted
    on the new box's first ship (session 169212e2, 2026-09-12).

    WHOSE box it lands on decides whether it moves now (`status: moved`) or asks
    (`status: pending`): onto a runner you administer, or between two runners with
    the SAME owner, it moves now. Onto someone else's box it becomes a transfer
    request that one of `approvers` must approve (`approve_transfer_request`),
    because the move spends their machine and Claude subscription. `runner` is the
    target's id or name.

    409, not 422, while a turn executes: the request is well-formed and will
    succeed once the source box is idle, which is a state conflict rather than a
    bad body. Stop the session (`POST /{id}/stop`) and retry. Also 409 while
    another request for this session is waiting.
    """
    session = _session_or_404(request, session_id)
    # Its writers, or an admin of the box it is on (`access.may_move`).
    if not access.may_move(request.user, session):
        raise HttpError(403, "you can read this session but not move it")
    try:
        req, moved = transfer_requests.request(
            session=session, runner_value=payload.runner, brief=payload.brief,
            user=request.user, initiator=who.for_request(request, via="transfer"),
            cli=payload.cli or "",
        )
    except LookupError as exc:
        raise HttpError(404, str(exc))
    except (RuntimeError, FileExistsError) as exc:
        raise HttpError(409, str(exc))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    if moved is None:
        return {
            "session_id": str(session.id), "runner": "", "transferred_from": "",
            "index_offset": 0, "turn_id": "", "status": "pending", "request_id": req.id,
            "approvers": sorted(u.email for u in transfer_requests.approvers(req.to_runner)),
        }
    return _moved_out(session, moved, req)


@router.get("/{session_id}/handoffs", response=list[HandoffOut],
            summary="What each transfer of this session carried")
@gate("session-acl")  # read
def list_handoffs(request: HttpRequest, session_id: uuid.UUID):
    """Every transfer of this session, newest first: what the source packed (or
    why it could not — NO SNAPSHOT), whether the target restored it and `git
    status` matched, and whether the source's task was closed."""
    session = _session_or_404(request, session_id)
    return [handoff.summary(h) for h in
            session.handoffs.select_related("source_runner", "target_runner")[:50]]


@router.post("/{session_id}/handoffs/{handoff_id}/restore", response=HandoffOut,
             summary="Restore a snapshot that arrived after its transfer")
@gate("session-acl", kind="conversation")  # write
def restore_handoff(request: HttpRequest, session_id: uuid.UUID, handoff_id: uuid.UUID):
    """A source that was asleep at the transfer packs its work when it is back.
    This puts that snapshot into the session now: a turn on the session's
    current runner restores it — into a worktree with no uncommitted changes
    only; the report says otherwise and leaves the worktree alone. 409 while a
    turn is queued or running in the session."""
    session = _session_or_404(request, session_id, write=True)
    h = get_object_or_404(session.handoffs.select_related("source_runner", "target_runner"),
                          pk=handoff_id)
    try:
        handoff.restore_late(h, user=request.user,
                             initiator=who.for_request(request, via="transfer"))
    except RuntimeError as exc:
        raise HttpError(409, str(exc))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    h.refresh_from_db()
    return handoff.summary(h)


def _moved_out(session, moved, req) -> dict:
    binding, turn = moved
    h = session.handoffs.select_related("source_runner", "target_runner").first()
    return {
        "session_id": str(session.id),
        "runner": binding.runner.name if binding.runner_id else "",
        "transferred_from": (
            binding.transferred_from.name if binding.transferred_from_id else ""
        ),
        "index_offset": binding.index_offset,
        "turn_id": str(turn.id) if turn else "",
        "status": "moved",
        "request_id": req.id,
        "handoff": handoff.summary(h) if h is not None else None,
    }
