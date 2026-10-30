"""The runner's side of a turn's connections, under `/api/harness/runners/{rid}/turns/{tid}/`.

Mounted at `/harness` (`apps/api/api.py`) so the URLs sit beside the turn's other
credentials, while the code stays here: harness and agents are core and may not
import connections. The turn lookup and the runner-holds-agent re-check mirror
`turn_github_token`, and are shared by both routes (`_turn_agent`).
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.http import HttpRequest
from ninja import Router, Schema
from ninja.errors import HttpError
from pydantic import Field

from apps.agents.credentials import delegations
from apps.agents.services import runner_may_hold_agent
from apps.api.errors import ProblemError
from apps.api.route_gates import gate
from apps.harness import routing
from apps.harness.api import runner_or_404
from apps.harness.models import Turn

from . import providers, services
from .problems import problem_for
from .schemas import AccessTokenOut, TurnConnectionsOut

router = Router(tags=["connections"])


class TurnConnectionTokenIn(Schema):
    provider: str
    min_life_seconds: int = Field(
        default=300, ge=0, le=3600,
        description="Hand out the cached token only if it lives at least this much longer; "
                    "else refresh first.",
    )
    refresh: bool = Field(
        default=False,
        description="Refresh even if the cached token looks fresh (the provider already rejected it).",
    )


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
    granted to it — so the runner fetches a token for each of those and calls
    nothing for an agent with none. No token here. 404 unless this runner holds
    the turn (always for a caller's turn); 409 `no_agent` for a turn that runs
    as no agent."""
    _, _, agent = _turn_agent(request, runner_id, turn_id)
    return TurnConnectionsOut(providers=services.providers_for_agent(agent))


@router.post("/runners/{runner_id}/turns/{turn_id}/connection-token", response=AccessTokenOut,
             summary="One claimed turn's agent connection token")
# turn must be claimed by this box
@gate("runner", "runner-holds-agent", kind="runner-protocol", step_up="runner protocol")
def turn_connection_token(request: HttpRequest, runner_id: uuid.UUID, turn_id: uuid.UUID,
                          payload: TurnConnectionTokenIn) -> AccessTokenOut:
    """A short-lived access token for the connection the turn's AGENT uses for
    `provider`: its own, else one granted to it — never the person who started
    the turn. 404 for an unknown provider, unless this runner holds the turn, and
    always for a caller's turn (one confined to a capability). 409 with `error`
    set to `no_agent`, `not_connected`, `ambiguous`, `reconnect_required` or
    `not_configured`; 502 `provider_error`, 503 `provider_unavailable`.
    `min_life_seconds` (default 300) is how long the token must still live;
    `refresh` forces a new one."""
    try:
        provider = providers.get_class(payload.provider)
    except providers.UnknownProvider:
        raise HttpError(404, "unknown provider") from None
    runner, turn, agent = _turn_agent(request, runner_id, turn_id)
    # No outer transaction: a refresh rejection stamps the row, and that stamp
    # must survive the exception turned into a 409 below.
    try:
        conn = services.connection_for_agent(agent, provider.name)
        token = services.issue_token(conn, to="turn", agent=agent.slug, turn=str(turn.pk),
                                     runner=str(runner.pk), runner_name=runner.name,
                                     min_life=timedelta(seconds=payload.min_life_seconds),
                                     refresh=payload.refresh)
    except services.ConnectionError as exc:
        raise problem_for(exc, title=provider.title, owner=f"{agent.slug}'s") from None
    return AccessTokenOut(access_token=token.token, instance_url=token.instance_url,
                          expires_at=token.expires_at, account_label=token.account_label)
