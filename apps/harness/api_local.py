"""Local use of an agent — a person's private runner and the local turns opened on it.

`/api/private-runners/` is the person's own (like their tokens): register a machine
once with your own canopy token (`canopy private-runner register`), list yours.
`/api/harness/local-turns/` is what the canopy plugin's SessionStart hook calls when
a session there starts using an agent: it opens (or re-opens) that session's turn
and hands back the caller envelope a runner would have received at claim.
Finishing it and uploading its transcript use the ordinary turn routes
(`/api/harness/turns/{id}/finish`, `/transcript`), which accept a local turn's own
initiator. Rules: `apps/harness/local.py`; canopy-web#1704.
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.workspaces import services as wsvc

from . import local
from .models import Turn, PrivateRunner
from .schemas import LocalTurnIn, LocalTurnOut, LocalTurnRenewOut, PrivateRunnerIn, PrivateRunnerOut

private_runners_router = Router(auth=session_auth, tags=["harness"])
local_turns_router = Router(auth=session_auth, tags=["harness"])


@private_runners_router.post("/", response={200: PrivateRunnerOut, 201: PrivateRunnerOut},
                          summary="Register this machine as one of your private runners")
def register_private_runner(request: HttpRequest, payload: PrivateRunnerIn):
    """Idempotent per (you, name): registering the same machine again returns it
    (and brings back a retired one). A private runner is yours, not an agent's or a
    workspace's — one serves every agent you use locally. It never claims turns."""
    try:
        ws, created = local.register_private_runner(request.user, name=payload.name, host=payload.host)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201 if created else 200, ws)


@private_runners_router.get("/", response=list[PrivateRunnerOut], summary="Your private runners")
def list_private_runners(request: HttpRequest):
    return list(PrivateRunner.objects.filter(owner=request.user, retired_at__isnull=True))


@private_runners_router.post("/{private_runner_id}/retire", response=PrivateRunnerOut,
                          summary="Retire one of your private runners")
def retire_private_runner(request: HttpRequest, private_runner_id: uuid.UUID):
    ws = local.private_runner_of(request.user, private_runner_id)
    if ws is None:
        raise HttpError(404, "private runner not found")
    ws.retired_at = timezone.now()
    ws.save(update_fields=["retired_at"])
    return ws


def _local_turn_or_404(request: HttpRequest, turn_id: uuid.UUID) -> Turn:
    turn = Turn.objects.filter(pk=turn_id, origin=Turn.ORIGIN_LOCAL).select_related("agent").first()
    if turn is None or not local.is_initiator(turn, request.user):
        raise HttpError(404, "turn not found")
    return turn


@local_turns_router.post("/", response={200: LocalTurnOut, 201: LocalTurnOut},
                         summary="Open the local turn for a Claude Code session on your private runner")
def open_local_turn(request: HttpRequest, payload: LocalTurnIn):
    """A session on your private runner is using `agent`: record it as a turn.

    Allowed when `access.decide` gives you the WHOLE agent (its owner or an admin,
    a `full:` rule in its caller list, or a workspace editor); a viewer or
    contact gets 403 with what to ask for — they use the agent through canopy, on
    its confined `ask` path. The turn runs `manual` unless you are an agent admin
    and ask for `auto`. Idempotent per Claude session: a resumed session re-opens
    its live turn. The response carries the caller envelope."""
    from apps.agents.services import get_agent

    from .caller_context import build

    agent = get_agent(payload.agent)
    # A non-member gets the same 404 as a missing agent (no existence leak).
    if agent is None or not wsvc.is_member(request.user, agent.workspace_id):
        raise HttpError(404, f"agent '{payload.agent}' not found")
    ws = local.private_runner_of(request.user, payload.private_runner)
    if ws is None:
        raise HttpError(404, "private runner not found — register it with `canopy private-runner register`")
    try:
        turn, created = local.open_turn(
            request, agent, private_runner=ws, claude_session_id=payload.claude_session_id,
            cwd=payload.cwd, requested_mode=payload.turn_mode)
    except local.LocalRefusedError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201 if created else 200, {
        "turn_id": turn.pk, "created": created, "agent": agent.slug,
        "workspace": agent.workspace_id, "turn_mode": turn.turn_mode,
        "turn_mode_basis": turn.turn_mode_basis, "lease_expires_at": turn.lease_expires_at,
        "url": wsvc.scoped_url(agent.workspace, f"/agents/{agent.slug}/turns?turn={turn.pk}"),
        "envelope": build(turn, reader_user=request.user),
    })


@local_turns_router.post("/{turn_id}/renew", response=LocalTurnRenewOut,
                         summary="Keep your local turn's lease alive")
def renew_local_turn(request: HttpRequest, turn_id: uuid.UUID):
    turn = local.renew(_local_turn_or_404(request, turn_id))
    return {"turn_id": turn.pk, "status": turn.status, "lease_expires_at": turn.lease_expires_at}
