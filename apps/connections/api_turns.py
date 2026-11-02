"""The runner's side of a turn's connections, under `/api/harness/runners/{rid}/turns/{tid}/`:
which providers to stage, and each one's material (route 11).

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

from . import proofs, providers, services, tokens
from .problems import problem_for, problem_for_wire
from .schemas import ConnectionMaterialOut, TurnConnectionsOut
from .wire import WireError

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


@router.get("/runners/{runner_id}/turns/{turn_id}/connections/{provider}", response=ConnectionMaterialOut,
            summary="A claimed turn's agent connection: sealed envelopes and the app client")
# turn must be claimed by this box, and the box must prove its device key
# a read: no write kind (the route-gate lint refuses one on a GET)
@gate("runner", "runner-holds-agent", step_up="runner protocol: ciphertext and an app client for a box")
def turn_connection_material(request: HttpRequest, runner_id: uuid.UUID, turn_id: uuid.UUID,
                             provider: str) -> ConnectionMaterialOut:
    """What this box needs to stage the turn's AGENT connection for `provider` (its
    own, else one lent to it — never the person who started the turn): the sealed
    envelopes, which only machines they are sealed to can open, the owner's signed
    grant statement and endorsements to check them by, and the app's client id and
    secret to refresh with. Never a token. Requires a device proof (`DPoP`) by this
    runner's own approved key (401 `device_proof_required`). 404 unless this runner
    holds the turn (always for a caller's turn), or for an unknown provider. 409
    `not_connected` when the agent has none, it is not shared yet, it is being
    disconnected, or this box no longer serves an agent it is lent to;
    `needs_reconnect` when its refresh token is dead; `waiting` when this box may
    use it but holds no copy yet; `ambiguous`, `no_agent`. Each turn's reads are
    recorded once (`token_issued`, naming the runner)."""
    try:
        cls = providers.get_class(provider)
    except providers.UnknownProvider:
        raise HttpError(404, "unknown provider") from None
    runner, turn, agent = _turn_agent(request, runner_id, turn_id)
    try:
        key = proofs.proving_device(request, runner)
    except WireError as exc:
        raise problem_for_wire(exc) from None
    try:
        conn = services.connection_for_agent(agent, cls.name)
    except services.ConnectionError as exc:
        raise problem_for(exc, title=cls.title, owner=f"{agent.slug}'s") from None
    try:
        return ConnectionMaterialOut(**tokens.material(conn, key, turn=turn, runner=runner))
    except WireError as exc:
        raise problem_for_wire(exc) from None
