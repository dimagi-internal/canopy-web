"""`POST /api/harness/runners/{rid}/turns/{tid}/connection-token` — an agent's connection token for one turn.

Mounted at `/harness` (`apps/api/api.py`) so the URL sits beside the turn's other
credentials, while the code stays here: harness and agents are core and may not
import connections. The turn lookup and the runner-holds-agent re-check mirror
`turn_github_token`.
"""
from __future__ import annotations

import logging
import uuid

from django.http import HttpRequest
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.agents.credentials import delegations
from apps.agents.services import runner_may_hold_agent
from apps.api.errors import ProblemError
from apps.api.route_gates import gate
from apps.events import services as events
from apps.harness import routing
from apps.harness.api import runner_or_404
from apps.harness.models import Turn

from . import providers, services
from .schemas import AccessTokenOut

logger = logging.getLogger(__name__)

router = Router(tags=["connections"])


class TurnConnectionTokenIn(Schema):
    provider: str


@router.post("/runners/{runner_id}/turns/{turn_id}/connection-token", response=AccessTokenOut,
             summary="One claimed turn's agent connection token (Salesforce)")
# turn must be claimed by this box
@gate("runner", "runner-holds-agent", kind="runner-protocol", step_up="runner protocol")
def turn_connection_token(request: HttpRequest, runner_id: uuid.UUID, turn_id: uuid.UUID,
                          payload: TurnConnectionTokenIn) -> AccessTokenOut:
    """A short-lived access token for the connection the turn's AGENT uses: its
    own, else one granted to it — never the person who started the turn. 404 unless
    this runner holds the turn, and always for a caller's turn (one confined to a
    capability). 409 with `error` set to `no_agent`, `not_connected`, `ambiguous`,
    `reconnect_required` or `not_configured`; 502/503 when the provider fails."""
    try:
        providers.get(payload.provider)
    except providers.UnknownProvider:
        raise HttpError(404, "unknown provider") from None
    runner = runner_or_404(request, runner_id)
    turn = (
        Turn.objects.select_related("agent__workspace", "chat_session__agent__workspace",
                                    "initiator_user", "initiator_contact")
        .filter(pk=turn_id, claimed_by=runner, status__in=routing.EXECUTING)
        .first()
    )
    # A caller's turn (confined to a capability) is steered by a stranger: it is
    # never lent the agent's Salesforce identity. Same 404 as a turn not held.
    if turn is None or turn.capability:
        raise HttpError(404, "turn not found")
    agent = delegations.turn_agent(turn)
    # A claim already requires this; re-asked because an owner can be demoted mid-turn.
    if agent is not None and not runner_may_hold_agent(runner, agent):
        raise HttpError(404, "turn not found")
    if agent is None:
        raise ProblemError(409, "No agent for this turn", detail="This turn runs as no agent.",
                           members={"error": "no_agent"})
    provider = payload.provider
    try:
        conn = services.connection_for_agent(agent, provider)
    except services.NotConnected:
        raise ProblemError(409, "Not connected", detail=f"{agent.slug} has no {provider} connection.",
                           members={"error": "not_connected"}) from None
    except services.ConnectionError:
        raise ProblemError(409, "More than one account",
                           detail=f"{agent.slug} has more than one {provider} connection.",
                           members={"error": "ambiguous"}) from None
    # No outer transaction: a refresh rejection stamps the row, and that stamp
    # must survive the exception turned into a 409 below.
    try:
        token = services.access_token_for(conn)
    except services.NotConfigured:
        raise ProblemError(409, "Not set up", detail=f"{provider} is not set up on this deployment.",
                           members={"error": "not_configured"}) from None
    except services.ReconnectRequired:
        raise ProblemError(409, "Reconnect required", detail=f"{agent.slug}'s {provider} connection no longer works.",
                           members={"error": "reconnect_required"}) from None
    except services.ConnectionError as exc:
        if exc.code == "provider_unavailable":
            raise ProblemError(503, "Provider unavailable", detail=f"{provider} could not be reached.") from None
        raise ProblemError(502, "Provider error", detail=f"{provider} refused the request.") from None
    try:
        events.record([{
            "source": services.EVENT_SOURCE, "kind": "connections.token_issued", "level": "info",
            "key": f"{agent.slug}:{turn.pk}",
            "summary": f"{agent.slug}: {provider} token for one turn on {runner.name}",
            "payload": {"agent": agent.slug, "turn": str(turn.pk), "runner": str(runner.pk),
                        "provider": provider, "account_label": token.account_label},
        }], workspace=agent.workspace)
    except Exception:  # noqa: BLE001 - an audit hiccup must not deny a turn its token
        logger.exception("could not record connections.token_issued for %s", agent.slug)
    return AccessTokenOut(access_token=token.token, instance_url=token.instance_url,
                          expires_at=token.expires_at, account_label=token.account_label)
