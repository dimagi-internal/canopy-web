"""`POST /api/harness/runners/{rid}/turns/{tid}/agent-token` — an agent login for one turn.

The runner that holds a turn asks, with its OWN runner token, for a login as the
turn's agent; it puts it where that turn's session acts as the agent, in place of
the stored `canopy-pat`. Who gets one and when it dies: `turn_agent_tokens`.
Spec: docs/superpowers/specs/2026-10-21-turn-scoped-agent-token-design.md.
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.errors import ProblemError
from apps.api.route_gates import gate

from . import turn_agent_tokens
from .models import Turn
from .routing import EXECUTING
from .schemas import TurnAgentTokenOut

router = Router(tags=["harness"])


@router.post("/{runner_id}/turns/{turn_id}/agent-token", response=TurnAgentTokenOut,
             summary="A login as the turn's agent, for one turn this runner holds")
# only the box's own runner token (a PAT is 403), turn claimed by it and executing
@gate("runner", "runner-holds-agent", kind="runner-protocol", step_up="runner protocol")
def turn_agent_token(request: HttpRequest, runner_id: uuid.UUID, turn_id: uuid.UUID, scope: str = "turn"):
    """A canopy login as the turn's agent, for ONE turn this runner is executing:
    expires within 12 hours and is revoked when the turn stops executing here. On a
    laptop runner, where a turn finishes as soon as its session exists, a finished
    turn whose session is still active and bound here gets a SESSION-scoped login
    instead (`session_id` set), revoked when that session leaves this runner.
    `?scope=session` asks for that session-scoped login only (404 when the turn's
    session is not live here), for a client that outlives the turn.
    Only with this runner's own runner token — a personal or OAuth token is
    refused, its owner's included. 404 unless this runner holds the turn and may
    hold its agent; 409 with the reason for a direct turn, a caller's (confined)
    turn, or an agent with no active login. Each issue is logged with the
    agent's credential reads."""
    # The box's own credential, bound to this runner — never a person's token: a
    # PAT of the runner's owner would let anyone holding it mint agent logins.
    rtok = getattr(request, "runner_token", None)
    if rtok is None:
        raise ProblemError(403, "A runner token is required",
                           detail="only a runner, signed in with its device key, may ask "
                                  "for a turn's agent login")
    if str(rtok.runner_id) != str(runner_id):
        raise HttpError(404, "turn not found")
    runner = rtok.runner
    # Only a turn this runner holds right now. A finished turn, or one another box
    # claimed, gets the same 404 as one that does not exist.
    turns = Turn.objects.select_related("agent__user", "agent__workspace",
                                        "chat_session__agent__user", "chat_session__agent__workspace")
    turn = turns.filter(pk=turn_id, claimed_by=runner, status__in=EXECUTING).first()
    session = None
    if scope == "session":
        # Asked for a login that lasts the SESSION (a laptop MCP connection outlives its
        # turn): only that, never a turn login that dies at turn end.
        turn = turns.filter(pk=turn_id, claimed_by=runner).first()
        session = turn_agent_tokens.live_session(turn, runner) if turn is not None else None
        if session is None:
            raise HttpError(404, "no live session for this turn on this runner")
    elif turn is None:
        # A laptop turn finishes the moment its session exists: its login then follows
        # that session while it stays live on this runner (session-scoped).
        turn = turns.filter(pk=turn_id, claimed_by=runner).first()
        session = turn_agent_tokens.live_session(turn, runner) if turn is not None else None
        if session is None:
            raise HttpError(404, "turn not found")
    from apps.agents.services import runner_may_hold_agent

    # A claim already requires this; re-asked because an owner can be demoted mid-turn.
    agent = turn_agent_tokens.turn_agent(turn)
    if agent is not None and not runner_may_hold_agent(runner, agent):
        raise HttpError(404, "turn not found")
    try:
        if session is not None:
            return turn_agent_tokens.issue_for_session(turn, runner, session)
        return turn_agent_tokens.issue(turn, runner)
    except turn_agent_tokens.Refused as exc:
        raise ProblemError(409, "No agent login for this turn", detail=str(exc)) from exc
