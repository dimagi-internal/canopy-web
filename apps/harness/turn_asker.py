"""An agent acting ON BEHALF OF the person who asked — worked out by canopy-web, never declared.

An agent's turn does the work of the person who started it. When that agent calls canopy
with the login canopy-web issued for the turn (`TurnAgentToken`), the request carries that
person: canopy-web works out who they are from the login alone, on every request, and
judges the decisions below on their access. Whatever turn the agent starts from there
(a dispatch, a send into another session, a new session) is stamped with them
(`origin_ref["on_behalf_of"]`, `services.enqueue_turn`), and the next agent's envelope
names them (`initiator.describe`). Nothing depends on an agent remembering to say it:
2026-10-26, ada re-ran ACE turns Jonathan had asked for, and ACE was told only "ada,
admin".

The person is found only when ALL of these hold (`_check`); otherwise the request is the
agent acting for itself, exactly as before:
* the request authenticated with a login canopy-web issued for one turn — never a stored
  PAT, which anything holding it could use to name any turn;
* the turn that login serves NOW — its own turn, or for a session login the session's
  newest turn (each message is its own turn) — is still executing, is not confined to a
  caller capability, and runs for this agent;
* that turn was started by a verified, active human (not canopy, not an agent), or was
  itself started on such a person's behalf (the chain carries them), and the request did
  not put itself in a lower role (`acts_as` only lowers).
The person's own turn cap (their `acts_as`) applies to them here as on their turn.

`X-Canopy-On-Behalf-Of: <email>` is still accepted, as an assertion: it must name that
same person, or the request is refused (403, saying why). It adds nothing a request
without it does not get.

What it decides: where the person's work runs (`access.may_pin_runner` — every door that
picks a runner) and whether a dispatch may run `auto` (`turn_gates`). Reads are unchanged:
a turn already reads as its asker wherever that matters (scoped caller tokens). Every
decision made on someone's behalf is recorded: an event, and the turn's
`origin_ref["on_behalf_of"]`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.agents.models import Agent
from apps.events import services as events
from apps.workspaces import role_cap
from apps.tokens import kinds

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
    """Check the request's `X-Canopy-On-Behalf-Of` against the person its login works for,
    and attach them to `user`. Raises Refused."""
    email = (email or "").strip().lower()
    if not email:
        raise Refused("X-Canopy-On-Behalf-Of names no one")
    ob = _check(user, credential, email)
    setattr(user, ATTR, ob)
    return ob


def attach(user, credential: dict | None) -> OnBehalf | None:
    """Work out the person `user`'s turn login works for and attach them; None (and nothing
    attached) when it works for no one — a stored token, canopy's own turn, an agent's."""
    kind = kinds.kind_of(credential or {})
    if kind is None or not kind.may_act_on_behalf:
        return None  # not a turn's login: no lookup at all
    try:
        ob = _check(user, credential, None)
    except Refused:
        return None
    setattr(user, ATTR, ob)
    return ob


def resolve(user, credential: dict | None, email: str) -> OnBehalf:
    """`declare` without attaching: the person, checked against `email`. Raises Refused."""
    email = (email or "").strip().lower()
    if not email:
        raise Refused("X-Canopy-On-Behalf-Of names no one")
    return _check(user, credential, email)


def _check(user, credential: dict | None, email: str | None) -> OnBehalf:
    cred = credential or {}
    kind = kinds.kind_of(cred)
    if kind is None or not kind.may_act_on_behalf or not cred.get("id"):
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
    person = _principal(current)
    if person is None:
        raise Refused("the turn this login serves was not started by a person")
    if email is not None and person.email.lower() != email:
        raise Refused(f"the turn this login serves was started by {person.email}, not {email}")
    acting_cap(current)  # their own turn's acts_as applies to them here too
    return OnBehalf(user=person, turn=current, agent=agent)


def _principal(turn: Turn):
    """The person `turn` does the work of: its verified, active human asker; or, for a turn
    an agent started, the person that agent was already acting for (stamped by the server
    when it started it, so a chain of dispatches keeps them). None for anyone else."""
    if turn.initiator_kind == who.USER and turn.initiator_user_id:
        person = turn.initiator_user
        if _is_person(person):
            if not is_verified(turn):
                raise Refused(f"the turn's asker {person.email} is not verified")
            return person
    ref = turn.origin_ref if isinstance(turn.origin_ref, dict) else {}
    carried = ref.get("on_behalf_of")
    if isinstance(carried, dict) and carried.get("user_id"):
        person = get_user_model().objects.filter(pk=carried["user_id"]).first()
        if person is not None and _is_person(person):
            return person
    return None


def _is_person(user) -> bool:
    return bool(user is not None and user.is_active
                and not Agent.objects.filter(user_id=user.pk).exists())


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
