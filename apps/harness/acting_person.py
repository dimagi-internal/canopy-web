"""Is the principal behind this request a PERSON acting as themselves — never an agent?

canopy tells a host "this is the person's own View, no agent acting" with a signed
statement (`Canopy-View`, `apps/host_grants/host_gateway.py`), and a host may give that
View more than it gives an agent (Connect Labs: the rows of a sensitive result). So the
question is asked of the PRINCIPAL, from the credential the request authenticated with —
never of the transport: MCP is the REST API, and a door is not who is asking.

A person acting as themselves: a browser session, a site's delegated (widget) token, a
contact's token, the person's own PAT or OAuth sign-in. Never one of these, whatever
door it uses:

* a conversation's caller token — a turn acting as the person who asked;
* a login canopy-web issued for one turn (`TurnAgentToken`), or any request naming the
  turn it runs in (`X-Canopy-Turn-Id`);
* an agent's own canopy login (`Agent.user`);
* a request declaring it acts on someone's behalf (`X-Canopy-On-Behalf-Of`) — that is an
  agent, however well it is vouched for;
* a runner's own credential.

Anything it cannot place is not a person (`refusal` names why).
"""
from __future__ import annotations

from apps.agents.models import Agent
from apps.tokens import kinds

from . import turn_asker
from .models import TurnAgentToken

TURN_HEADER = "HTTP_X_CANOPY_TURN_ID"


def refusal(request) -> str | None:
    """Why the principal behind `request` is not a person acting as themselves, or None."""
    if getattr(request, "runner_token", None) is not None:
        return "runner"
    meta = getattr(request, "META", None) or {}
    if meta.get(TURN_HEADER):
        return "turn"
    if getattr(request, "contact", None) is not None:
        # A contact's token is the visitor's own, minted by the site for them; no turn
        # or agent is ever given one.
        return None
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return "anonymous"
    cred = getattr(request, "auth_credential", None) or {}
    if kinds.speaks_for_a_turn(request) or cred.get("turn_id"):
        return "turn"
    if turn_asker.of(user) is not None:
        return "on_behalf"
    if Agent.objects.filter(user_id=user.pk).exists():
        return "agent_login"
    if cred.get("type") == kinds.PAT and cred.get("id"):
        if TurnAgentToken.objects.filter(token_id=cred["id"]).exists():
            return "turn_login"
    return None


def is_person(request) -> bool:
    return refusal(request) is None
