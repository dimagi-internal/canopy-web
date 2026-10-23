"""An agent acting ON BEHALF OF the person who asked — said on the request, checked here.

An agent's turn acts as the agent (access.md). Most of the time that is right: hal doing
its own work is judged as hal. But when the person in the turn asks for something that is
theirs to decide — "republish it on cloud-ec2-2", their box, the agent they own — the
agent is doing THEIR work, and judging it as the agent asks the wrong question. So the
agent says which it is doing, on the request:

    X-Canopy-On-Behalf-Of: jjackson@dimagi.com

and canopy-web checks the claim and, if it holds, judges the decisions below on that
person's access instead of the agent's. A request without the header is the agent acting
for itself, under its own access, exactly as before. Nothing is inferred.

The claim holds only when ALL of these do (`resolve`; any failure is a 403 that says which):
* the request authenticated with a login canopy-web issued for one turn
  (`TurnAgentToken`) — never a stored PAT, which anything holding it could use to name
  any turn;
* the turn that login serves NOW — its own turn, or for a session login the session's
  newest turn (each message is its own turn) — is still executing, is not confined to a
  caller capability, and runs for this agent;
* that turn was started by the named person, a verified, active human (not canopy, not
  an agent), and the request did not put itself in a lower role (`acts_as` only lowers).
The person's own turn cap (their `acts_as`) applies to them here as on their turn.

What it decides: where the person's work runs (`access.may_pin_runner` — every door that
picks a runner) and whether a dispatch may run `auto` (`turn_gates`). Reads are unchanged:
a turn already reads as its asker wherever that matters (scoped caller tokens). Every
decision made on someone's behalf is recorded: an event, and on a dispatched turn
`origin_ref["on_behalf_of"]`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db.models import Q

from apps.agents.models import Agent
from apps.events import services as events
from apps.workspaces import role_cap

from . import initiator as who
from .caller_context import acting_cap, is_verified
from .models import Turn, TurnAgentToken
from .routing import EXECUTING
from .turn_agent_tokens import turn_agent

logger = logging.getLogger(__name__)

#: The request header an agent names the person it acts for with (their email).
HEADER = "HTTP_X_CANOPY_ON_BEHALF_OF"
#: On the request's user: the `OnBehalf` the request declared and canopy-web accepted.
ATTR = "_canopy_on_behalf_of"


class Refused(Exception):
    """The request said it acts for someone, and it may not; the message says why."""


@dataclass
class OnBehalf:
    """The person an agent's request acts for, and the turn that lets it."""

    user: object
    turn: Turn
    agent: Agent
    #: What their access decided on this request, for the record (`note`).
    decided: list[str] = field(default_factory=list)

    def record(self) -> dict:
        return {"user_id": self.user.pk, "email": self.user.email, "turn": str(self.turn.pk),
                "agent": self.agent.slug, "decided": list(self.decided)}


def of(user) -> OnBehalf | None:
    """The person `user`'s request declared it acts for (and canopy-web accepted), or None
    when it acts for itself."""
    return getattr(user, ATTR, None) if user is not None else None


def subject(user):
    """Whose access a decision is judged on: the person the request acts for, else the
    caller itself."""
    ob = of(user)
    return ob.user if ob is not None else user


def declare(user, credential: dict | None, email: str) -> OnBehalf:
    """Check the request's claim to act for `email` and attach it to `user`. Raises Refused."""
    ob = resolve(user, credential, email)
    setattr(user, ATTR, ob)
    return ob


def resolve(user, credential: dict | None, email: str) -> OnBehalf:
    email = (email or "").strip().lower()
    if not email:
        raise Refused("X-Canopy-On-Behalf-Of names no one")
    cred = credential or {}
    if cred.get("type") != "pat" or not cred.get("id"):
        raise Refused("acting on someone's behalf needs the login canopy-web issued for their "
                      "turn; this request used another credential")
    if role_cap.cap_of(user):
        raise Refused("a request in a lower role (acts_as) cannot act on someone's behalf")
    grant = (TurnAgentToken.objects.select_related("turn", "turn__agent", "session")
             .filter(token_id=cred["id"], token__user_id=user.pk).first())
    if grant is None:
        raise Refused("acting on someone's behalf needs the login canopy-web issued for their "
                      "turn; this is a stored token, not a turn's login")
    agent = turn_agent(grant.turn)
    if agent is None or not agent.is_own_login(user):
        raise Refused("this login does not act as the agent its turn runs for")
    current = _current_turn(grant)
    if current is None or current.status not in EXECUTING:
        raise Refused("the turn this login serves is no longer running, so it acts for no one")
    if current.capability:
        raise Refused("a turn confined to a caller capability acts for no one but itself")
    acting = turn_agent(current)
    if acting is None or acting.pk != agent.pk:
        raise Refused("the turn this login serves now runs for another agent")
    person = current.initiator_user if current.initiator_user_id else None
    if (person is None or current.initiator_kind != who.USER or not person.is_active
            or Agent.objects.filter(user_id=person.pk).exists()):
        raise Refused("the turn this login serves was not started by a person")
    if not is_verified(current):
        raise Refused(f"the turn's asker {person.email} is not verified")
    if person.email.lower() != email:
        raise Refused(f"the turn this login serves was started by {person.email}, not {email}")
    acting_cap(current)  # their own turn's acts_as applies to them here too
    return OnBehalf(user=person, turn=current, agent=agent)


def _current_turn(grant: TurnAgentToken) -> Turn | None:
    """The turn the login serves NOW: its own, or for a session login the session's newest
    turn — the person's latest message."""
    if grant.session_id is None:
        return grant.turn
    sid = str(grant.session_id)
    return (Turn.objects.select_related("agent", "chat_session__agent", "initiator_user")
            .filter(Q(chat_session_id=grant.session_id) | Q(origin_ref__chat_session_id=sid))
            .order_by("-created_at").first())


def note(ob: OnBehalf, decision: str) -> None:
    """Record that `decision` was made on `ob.user`'s access. Never raises."""
    if decision not in ob.decided:
        ob.decided.append(decision)
    try:
        events.record([{
            "source": "harness.turn_asker",
            "kind": "agent.on_behalf.decided",
            "level": "info",
            "key": f"{ob.agent.slug}:{ob.turn.pk}:{decision}",
            "summary": f"{ob.agent.slug}, acting for {ob.user.email} in their turn: {decision}",
            "payload": {"agent": ob.agent.slug, "turn": str(ob.turn.pk), "on_behalf_of": ob.user.email,
                        "decision": decision},
        }], workspace=ob.agent.workspace)
    except Exception:  # noqa: BLE001 — the record must not undo the decision
        logger.exception("on behalf: could not record %s for %s", decision, ob.turn.pk)
