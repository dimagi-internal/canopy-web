"""Is the principal behind this request a PERSON acting as themselves — never an agent?

canopy tells a host "this is the person's own View, no agent acting" with a signed
statement (`Canopy-View`, `apps/host_grants/host_gateway.py`), and a host may give that
View more than it gives an agent (Connect Labs: the rows of a sensitive result). So the
question is asked of the PRINCIPAL, from the credential the request authenticated with —
never of the transport: MCP is the REST API, and a route is not who is asking.

A person acting as themselves: a browser session, a site's delegated (widget) token, a
contact's token, the person's own PAT or OAuth sign-in. Never one of these, whatever
route it uses:

* a conversation's caller token — a turn acting as the person who asked;
* a login canopy-web issued for one turn (`TurnAgentToken`), or any request naming the
  turn it runs in (`X-Canopy-Turn-Id`);
* an agent's own canopy login (`Agent.user`);
* a request declaring it acts on someone's behalf (`X-Canopy-On-Behalf-Of`) — that is an
  agent, however well it is vouched for;
* a runner's own credential.

Anything it cannot place is not a person (`refusal` names why).

`person_or_act_as` is the same question for a route that also lets a person's act be done
by an agent acting AS them under an act-as-you permission they approved with their passkey
(`act_as`): live, `write`, and naming no other turn. A route adopts it by choice; `refusal`
alone still refuses every act-as token.
"""
from __future__ import annotations

import uuid

from apps.agents.models import Agent
from apps.tokens import kinds

from . import act_as, turn_asker
from .models import ActAsGrant, Turn, TurnAgentToken

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


#: Why `person_or_act_as` refuses an act-as-you token, beyond `refusal`'s reasons.
ACT_AS_DEAD, ACT_AS_READ_ONLY, ACT_AS_MISMATCH = "act_as_dead", "act_as_read_only", "act_as_mismatch"


def act_as_grant(request):
    """The act-as-you permission `request` signed in with (`apps/harness/act_as.py`), or
    None. Only a request the token middleware authenticated with an act-as token (over
    REST, or an MCP call made with one) carries one."""
    grant = getattr(request, "act_as_grant", None)
    cred = getattr(request, "auth_credential", None) or {}
    if grant is None or kinds.kind_of(cred) is not kinds.KINDS[kinds.ACT_AS]:
        return None
    return grant


def _names_another_turn(request, grant: ActAsGrant) -> bool:
    """Does the request name a turn (`X-Canopy-Turn-Id`) the permission was not asked for?
    Its own turn passes, and so does a later turn of the conversation it was asked in (a
    chat's next message is a new turn, and the permission belongs to the conversation)."""
    named = ((getattr(request, "META", None) or {}).get(TURN_HEADER) or "").strip()
    if not named or named == str(grant.turn_id):
        return False
    if not grant.login_session_id:
        return True
    try:
        turn = Turn.objects.filter(pk=uuid.UUID(named)).first()
    except ValueError:
        return True
    return turn is None or act_as.session_of(turn) != grant.login_session_id


def person_or_act_as(request):
    """The person behind `request` for an act only a person may do: `(user, None)` for a
    person acting as themselves (`refusal` is None), `(person, grant)` for an agent acting
    AS that person under an act-as-you permission, or the reason it is neither (a str).

    The permission counts only when ALL of these hold: it is live (approved with the
    person's passkey, inside its 30 minutes, its turn or conversation still held —
    `act_as.dead_reason`, read again here), it grants `write`, and the request names no
    other turn. Its token signs in as the person, so whatever the route asks of
    `request.user` (agent-admin, say) is asked of the person. Every other principal
    `refusal` refuses stays refused: an agent's own login, a turn's login or token,
    `X-Canopy-On-Behalf-Of`, a runner, anonymous."""
    grant = act_as_grant(request)
    if grant is None:
        why = refusal(request)
        return why if why is not None else (request.user, None)
    if getattr(request, "runner_token", None) is not None:
        return "runner"
    user = getattr(request, "user", None)
    cred = getattr(request, "auth_credential", None) or {}
    if (user is None or user.pk != grant.person_id
            or str(cred.get("act_as_grant") or "") != str(grant.pk)):
        return ACT_AS_MISMATCH
    if _names_another_turn(request, grant):
        return "turn"
    if turn_asker.of(user) is not None:
        return "on_behalf"
    fresh = ActAsGrant.objects.select_related("person", "agent", "turn").filter(pk=grant.pk).first()
    if fresh is None or act_as.dead_reason(fresh) is not None:
        return ACT_AS_DEAD
    if fresh.scope != ActAsGrant.WRITE:
        return ACT_AS_READ_ONLY
    if Agent.objects.filter(user_id=user.pk).exists():
        return "agent_login"
    return user, fresh
