"""Django Ninja router for /api/canopy-sessions — live chat sessions.

Session-authed + workspace-membership gated. A "send" enqueues a session Turn;
in SP2a the stub executor runs it inline (the SP2b cloud runner will claim it
async instead — no API change when that lands).
"""
from __future__ import annotations

import uuid

from django.db.models import Max

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from ninja import File, Router
from ninja.errors import HttpError
from ninja.files import UploadedFile

from apps.agents import services as agent_services
from apps.api.auth import session_auth
from apps.api.pagination import clamp_limit
from apps.harness import services as harness_services
from apps.harness.models import Turn
from apps.workspaces import services as wsvc

from . import attachment_storage, services
from .models import Attachment, Session
from .schemas import (
    AttachmentOut,
    BackfillStateOut,
    MessageOut,
    MessagePageOut,
    PlaceIn,
    SendIn,
    SendOut,
    SessionCreateIn,
    SessionDetailOut,
    SessionOut,
    StreamStateOut,
    TurnOutMinimal,
)

router = Router(auth=session_auth, tags=["chat"])


def _runner_online(runner) -> bool | None:
    """Liveness of a session's bound runner, or None when there is no binding."""
    if runner is None:
        return None
    from apps.harness.models import Runner  # lazy: framework->framework import cycle

    return runner.live_status == Runner.ONLINE


def _out(session: Session) -> dict:
    binding = getattr(session, "runner_binding", None)  # reverse 1:1 -> None when absent
    runner = binding.runner if (binding and binding.runner_id) else None
    # The name a human recognises for a runner-bound session is the emdash
    # task (what they see in emdash), not a thread_key hash a fallback title
    # may have captured. Web chats keep their own title. Web-origin sessions
    # also prefer their own title once set (e.g. the server-side auto-titler)
    # over a bound session_key, since a web chat's binding is an execution
    # detail, not the identity the human gave the conversation.
    prefer_own = session.origin != Session.ORIGIN_RUNNER and bool(session.title)
    return {
        "id": session.id,
        "agent_slug": session.agent.slug if session.agent_id else None,
        "project": session.project,
        "workspace": session.workspace_id,
        "title": (
            session.title
            if prefer_own
            else ((binding.session_key if (binding and binding.session_key) else "") or session.title)
        ),
        "status": session.status,
        "created_at": session.created_at,
        # When it last DID something (binding > newest message > created).
        "last_activity_at": services.last_activity_at(session, binding),
        # --- liveness (Plan 4): one shape, computed from the binding ---
        "origin": session.origin,
        "running": services.is_session_running(binding),
        "runner_name": runner.name if runner else None,
        "runner_location": runner.location if runner else None,
        # See SessionOut.runner_online: an embedder's delegated user cannot list
        # runners, so the session payload is where they learn their bound runner
        # went away. None when unbound — nothing to be offline.
        "runner_online": _runner_online(runner),
        "session_key": binding.session_key if binding else "",
    }


def _visible_slugs(request: HttpRequest) -> set[str]:
    wsvc.auto_join_workspaces(request.user)
    pinned = getattr(request, "workspace_slug", None)
    return {pinned} if pinned else set(wsvc.user_workspace_slugs(request.user))


def _session_or_404(request: HttpRequest, session_id: uuid.UUID) -> Session:
    session = get_object_or_404(
        Session.objects.select_related("agent", "runner_binding", "runner_binding__runner")
        .annotate(_last_msg_at=Max("messages__created_at")),
        pk=session_id,
    )
    if session.workspace_id not in _visible_slugs(request):
        raise HttpError(404, "session not found")  # wrong tenant / non-member
    return session


def _set_status(request: HttpRequest, session_id: uuid.UUID, status: str) -> dict:
    session = _session_or_404(request, session_id)   # membership gate: non-member -> 404
    if session.status != status:
        session.status = status
        session.save(update_fields=["status", "updated_at"])
    return _out(session)


@router.post("/", response=SessionOut, summary="Create a chat session")
def create_session(request: HttpRequest, payload: SessionCreateIn):
    if payload.agent_slug and payload.project:
        raise HttpError(422, "a session targets an agent or a project, not both")
    try:
        workspace = wsvc.current_workspace(request.user, getattr(request, "workspace_slug", None))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    agent = None
    if payload.agent_slug:
        agent = agent_services.get_agent(payload.agent_slug)
        if agent is None or agent.workspace_id != workspace.slug:
            raise HttpError(404, f"agent '{payload.agent_slug}' not found in this workspace")
    metadata = dict(payload.metadata)
    if payload.runner_id:
        # Directed new chat: stashed for the session's first send to pin onto
        # (as long as it's still unbound at that point) — see services.send_message.
        metadata["requested_runner_id"] = str(payload.runner_id)
    session = services.create_session(
        workspace=workspace, created_by=request.user, agent=agent,
        project=payload.project, title=payload.title, metadata=metadata,
    )
    return _out(session)


@router.get("/", response=list[SessionOut], summary="List sessions (web + runner-discovered)")
def list_sessions(
    request: HttpRequest, state: str = "active", limit: int = 200,
    source: str = "", opp_slug: str = "", opp_run_id: str = "",
    origin_key: str = "",
):
    # The ONE unified list (Plan 4): every session the caller can see in their
    # workspaces — their own web sessions UNION any session that has a
    # RunnerBinding (runner-discovered or live). Deduped, running-first, then
    # newest. Replaces the creator-only list + the harness OpenSessions projection.
    #
    # `state` gives that list an END. Two rules combine into "archived":
    #   - WRITTEN: status == archived (the runner saw the emdash task archived, or
    #     a human called /archive). Durable.
    #   - DERIVED: a RUNNER-origin session whose binding has not been seen within
    #     SESSION_STALE_AFTER. Computed here, never stored, so it reverses itself
    #     the moment the task is reported again. Web sessions are exempt — they
    #     have no runner to be seen by, so only an explicit archive ends them.
    from django.db.models import Max, Q

    if state not in ("active", "archived", "all"):
        raise HttpError(422, "state must be one of: active, archived, all")

    slugs = _visible_slugs(request)
    rows = (
        Session.objects.select_related("agent", "runner_binding", "runner_binding__runner")
        .filter(workspace_id__in=slugs)
        .filter(Q(created_by=request.user) | Q(runner_binding__isnull=False))
    )
    # Embedder filters (Task 9): an embedder (e.g. ace-web) narrows the shared
    # session list to the sessions it cares about, keyed on the opaque
    # `metadata` bag a session carries (never interpreted elsewhere in this
    # app). Empty string = no filter, so the default call is unaffected.
    #
    # `origin_key` is the generic one: an embedder whose own product is
    # multi-tenant stamps ITS tenant into metadata.origin_key at create time and
    # filters on it here, so two of its tenants sharing one canopy workspace do
    # not see each other's sessions in the list. Deliberately opaque — canopy
    # never parses it. (Note the residual: this scopes the LIST; canopy's own
    # tenancy still lets any member of the canopy workspace open a session by id.
    # An embedder that needs hard isolation maps its tenants onto separate canopy
    # workspaces instead.)
    if source:
        rows = rows.filter(metadata__source=source)
    if origin_key:
        rows = rows.filter(metadata__origin_key=origin_key)
    if opp_slug:
        rows = rows.filter(metadata__opp_slug=opp_slug)
    if opp_run_id:
        rows = rows.filter(metadata__opp_run_id=opp_run_id)
    rows = rows.annotate(_last_msg_at=Max("messages__created_at")).distinct().order_by("-created_at")
    unseen = services.unseen_q()   # defined once in staleness.py; see Step 3
    if state == "active":
        rows = rows.filter(status=Session.ACTIVE).exclude(unseen)
    elif state == "archived":
        rows = rows.filter(Q(status=Session.ARCHIVED) | unseen)

    out = [_out(s) for s in rows]
    # Running first, then genuinely-most-recent. Sorting by created_at made a
    # dead repo and a live one interleave arbitrarily (both "created" in the
    # same report sweep); last_activity_at is the real signal. The client can
    # re-group by project — this is the default order.
    out.sort(key=lambda r: (not r["running"], -(r["last_activity_at"].timestamp())))
    # Clamp AFTER the sort, never as a queryset slice: the queryset is ordered by
    # -created_at, so slicing it could drop the running session this sort exists to
    # float. `state=active` already bounds the set; this is a payload backstop.
    return out[: clamp_limit(limit)]


@router.get("/{session_id}", response=SessionDetailOut, summary="Get a session + transcript tail")
def get_session(request: HttpRequest, session_id: uuid.UUID, full: bool = False):
    # Tail-first: never ship the whole transcript by default. The client gets the
    # last SESSION_TAIL_DEFAULT messages + a backward cursor; ?full=true is the
    # explicit escape hatch. Scroll-back pages via GET /{id}/messages?before=.
    session = _session_or_404(request, session_id)
    data = _out(session)
    rows, has_more, oldest = services.visible_transcript(session, full=full)
    data["messages"] = [MessageOut.from_orm(m) for m in rows]
    data["has_more_before"] = has_more
    data["oldest_loaded_turn_index"] = oldest
    return data


@router.get(
    "/{session_id}/messages",
    response=MessagePageOut,
    summary="Load earlier transcript (scroll-back)",
)
def list_messages(
    request: HttpRequest,
    session_id: uuid.UUID,
    before: int,
    limit: int = services.SCROLLBACK_PAGE_DEFAULT,
):
    # Cursor-based backward paging: the window of `limit` messages immediately
    # older than `before` (a turn_index), chronological, + whether older exists.
    # Clamp here (not in services.messages_before, which stays a pure helper) —
    # an unclamped `?limit=-1`/`0` hits `queryset[:limit]` and raises
    # ValueError("Negative indexing is not supported"), surfacing as a 500.
    session = _session_or_404(request, session_id)
    limit = clamp_limit(limit)
    rows, has_more = services.messages_before(session, before=before, limit=limit)
    return {
        "messages": [MessageOut.from_orm(m) for m in rows],
        "has_more_before": has_more,
    }


@router.post("/{session_id}/archive", response=SessionOut, summary="Archive a session")
def archive_session(request: HttpRequest, session_id: uuid.UUID):
    """Retire a session by hand. The escape hatch for a web chat — no runner will ever
    report it archived — and for force-retiring a row without touching emdash.
    Idempotent, and never destructive: /unarchive brings it straight back."""
    return _set_status(request, session_id, Session.ARCHIVED)


@router.post("/{session_id}/unarchive", response=SessionOut, summary="Unarchive a session")
def unarchive_session(request: HttpRequest, session_id: uuid.UUID):
    """Undo an archive. Note this clears only the WRITTEN half: a runner session that
    is also past SESSION_STALE_AFTER stays out of `state=active` until its runner
    reports it again, because that half is derived on every read."""
    return _set_status(request, session_id, Session.ACTIVE)


@router.post("/{session_id}/send", response=SendOut, summary="Send a message")
def send(request: HttpRequest, session_id: uuid.UUID, payload: SendIn):
    session = _session_or_404(request, session_id)
    if not payload.text.strip():
        raise HttpError(422, "message text is required")
    try:
        message, turn = services.send_message(
            session=session, text=payload.text, user=request.user,
            client_id=payload.client_id, placement=payload.placement,
        )
    except ValueError as exc:
        raise HttpError(422, str(exc))
    # Dev/test: run the stub inline. Production: leave it queued for a cloud runner.
    services.maybe_execute_inline(turn)
    return {"turn_id": turn.id if turn else None, "message": MessageOut.from_orm(message)}


@router.post(
    "/{session_id}/place", response=TurnOutMinimal,
    summary="Re-pin a session's oldest queued turn to a runner",
)
def place(request: HttpRequest, session_id: uuid.UUID, payload: PlaceIn):
    # The chat banner's after-the-fact directed-placement decision (vs. `runner_id`
    # on create / `placement` on send, which only apply to a turn at enqueue time).
    session = _session_or_404(request, session_id)
    try:
        turn = services.place_queued_turn(session=session, placement=payload.placement)
    except LookupError as exc:
        raise HttpError(404, str(exc))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    return turn


@router.post("/{session_id}/stop", response=dict, summary="Cancel every non-terminal turn on this session")
def stop_session_turn(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    # ALL non-terminal turns, not just the newest: a mid-reply send queues a
    # second turn behind the one still running, so Stop must reach both — the
    # running one gets cancel_requested, the queued one is finished CANCELLED.
    # NOTE: this must not be a bare `any(... for turn in turns)` — any() short-
    # circuits on the first truthy result, which would skip cancelling every
    # turn after the first non-None one.
    turns = Turn.objects.filter(chat_session=session, status__in=list(Turn.NON_TERMINAL))
    cancelled = False
    for turn in turns:
        if harness_services.cancel_turn(turn) is not None:
            cancelled = True
    return {"cancelled": cancelled}


@router.post("/{session_id}/attach", response=StreamStateOut, summary="Attach a viewer (start live streaming)")
def attach_session(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    return {"streaming": services.attach_session(session)}


@router.post("/{session_id}/detach", response=StreamStateOut, summary="Detach a viewer (stop when last leaves)")
def detach_session(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    return {"streaming": services.detach_session(session)}


@router.post("/{session_id}/backfill", response=BackfillStateOut, summary="Request full history from the runner")
def request_backfill(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    return {"status": services.request_backfill(session)}


@router.post(
    "/{session_id}/attachments",
    response={201: AttachmentOut},
    summary="Upload an attachment for this session",
)
def upload_attachment(
    request: HttpRequest, session_id: uuid.UUID, file: UploadedFile = File(...)
):
    """Store the bytes and return an id the caller passes to /send.

    UNBOUND on purpose (`message` null): the composer uploads while you are
    still typing, so the message it belongs to does not exist yet. Sending binds
    it. That ordering is also what lets the UI show a thumbnail before send.
    """
    session = _session_or_404(request, session_id)   # membership gate: non-member -> 404
    if not attachment_storage.is_configured():
        raise HttpError(503, "attachments are not configured on this deployment")

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    allowed = settings.ATTACHMENT_ALLOWED_CONTENT_TYPES
    if content_type not in allowed:
        # An allowlist: these bytes get opened by an agent and rendered inline by
        # a browser, so anything not explicitly understood is refused.
        raise HttpError(422, f"unsupported file type '{content_type or 'unknown'}'")
    if file.size is None or file.size <= 0:
        raise HttpError(422, "file is empty")
    if file.size > settings.ATTACHMENT_MAX_UPLOAD_BYTES:
        limit_mb = settings.ATTACHMENT_MAX_UPLOAD_BYTES // (1024 * 1024)
        raise HttpError(422, f"file is larger than the {limit_mb}MB limit")

    attachment = Attachment(
        session=session,
        uploaded_by=request.user,
        filename=attachment_storage.safe_filename(file.name),
        content_type=content_type,
        size_bytes=file.size,
    )
    attachment.storage_key = attachment_storage.storage_key(
        session.id, attachment.id, attachment.filename
    )
    # Bytes FIRST, row second: a row whose object is missing is a broken
    # thumbnail and a runner download that 500s, while an orphaned object is
    # invisible and sweepable. Fail in the harmless direction.
    attachment_storage.put(attachment.storage_key, file.read(), content_type)
    attachment.save()
    return 201, attachment


@router.get(
    "/attachments/{attachment_id}/content",
    summary="Stream an attachment's bytes",
)
def attachment_content(request: HttpRequest, attachment_id: uuid.UUID):
    """The bytes, for both readers: the browser rendering a thumbnail and the
    runner downloading into the agent's workspace (which authenticates with a
    PAT, resolved upstream into request.user like any other caller).

    Gated on session membership, not on who uploaded it — a session is
    multiplayer, so a teammate must be able to see what was shared in it.
    """
    attachment = get_object_or_404(
        Attachment.objects.select_related("session"), pk=attachment_id
    )
    if attachment.session.workspace_id not in _visible_slugs(request):
        raise HttpError(404, "attachment not found")  # wrong tenant / non-member
    if not attachment_storage.is_configured():
        raise HttpError(503, "attachments are not configured on this deployment")

    stored = attachment_storage.get(attachment.storage_key)
    response = HttpResponse(stored.body, content_type=stored.content_type)
    # inline: the browser renders it rather than downloading. filename is already
    # sanitised at upload, so it is safe in the header.
    response["Content-Disposition"] = f'inline; filename="{attachment.filename}"'
    return response


@router.delete("/attachments/{attachment_id}", response={204: None})
def delete_attachment(request: HttpRequest, attachment_id: uuid.UUID):
    """Remove an attachment you have not sent yet — the composer's "x" on a chip.

    Only while UNBOUND. Once it is part of a sent message it is transcript, and
    deleting it would leave the agent's reply referring to something nobody else
    can see.
    """
    attachment = get_object_or_404(
        Attachment.objects.select_related("session"), pk=attachment_id
    )
    if attachment.session.workspace_id not in _visible_slugs(request):
        raise HttpError(404, "attachment not found")
    if attachment.message_id is not None:
        raise HttpError(409, "this attachment has already been sent")
    if attachment_storage.is_configured():
        attachment_storage.delete(attachment.storage_key)
    attachment.delete()
    return 204, None
