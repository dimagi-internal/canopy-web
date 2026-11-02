"""The runner's side of a turn's connections, under `/api/harness/runners/{rid}/turns/{tid}/`.

Mounted at `/harness` (`apps/api/api.py`) so the URLs sit beside the turn's other
credentials, while the code stays here: harness and agents are core and may not
import connections. The turn lookup and the runner-holds-agent re-check mirror
`turn_github_token` (`_turn_agent`).
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents.credentials import delegations
from apps.agents.services import runner_may_hold_agent
from apps.api.errors import ProblemError
from apps.api.route_gates import gate
from apps.harness import routing
from apps.harness.api import runner_or_404
from apps.harness.models import Turn

from . import services
from .schemas import TurnConnectionsOut

router = Router(tags=["connections"])


def _turn_agent(request: HttpRequest, runner_id: uuid.UUID, turn_id: uuid.UUID):
    """(runner, turn, agent) for a turn this runner is executing, else 404; 409
    `no_agent` for a turn that runs as no agent."""
    runner = runner_or_404(request, runner_id)
    turn = (
        Turn.objects.select_related("agent__workspace", "chat_session__agent__workspace",
                                    "initiator_user", "initiator_contact")
        .filter(pk=turn_id, claimed_by=runner, status__in=routing.EXECUTING)
        .first()
    )
    # A caller's turn (confined to a capability) is steered by a stranger: it is
    # never lent the agent's identity anywhere. Same 404 as a turn not held.
    if turn is None or turn.capability:
        raise HttpError(404, "turn not found")
    agent = delegations.turn_agent(turn)
    # A claim already requires this; re-asked because an owner can be demoted mid-turn.
    if agent is not None and not runner_may_hold_agent(runner, agent):
        raise HttpError(404, "turn not found")
    if agent is None:
        raise ProblemError(409, "No agent for this turn", detail="This turn runs as no agent.",
                           members={"error": "no_agent"})
    return runner, turn, agent


@router.get("/runners/{runner_id}/turns/{turn_id}/connections", response=TurnConnectionsOut,
            summary="Which connections one claimed turn's agent can use")
# turn must be claimed by this box; a read that hands out no credential
@gate("runner", "runner-holds-agent")
def turn_connections(request: HttpRequest, runner_id: uuid.UUID, turn_id: uuid.UUID) -> TurnConnectionsOut:
    """The providers the turn's AGENT has a connection for — its own, or one
    granted to it — so the runner stages each of those and calls nothing for an
    agent with none. No token here. 404 unless this runner holds
    the turn (always for a caller's turn); 409 `no_agent` for a turn that runs
    as no agent."""
    _, _, agent = _turn_agent(request, runner_id, turn_id)
    return TurnConnectionsOut(providers=services.providers_for_agent(agent))
