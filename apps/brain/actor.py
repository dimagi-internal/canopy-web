"""Who may manage a workspace's learning: ONE rule, asked by every brain management route.

Configuring learning in a workspace (its "Learning reads", its learners, their targets, schedules and budgets, Run now,
a finding's status, a flag, and the brain page's reads) is the workspace's owners' and admins' (`perms.BRAIN_MANAGE`,
inherited from above) or a canopy admin's, and is open to two kinds of caller:

* the PERSON, as themselves (`person.refusal`: their browser session or a PAT they minted by hand, never over MCP);
* an ADMIN AGENT: the agent's own canopy login, inside its own running turn (the per-turn login canopy issued for it,
  `turn_asker.serving_turn`; never a stored token), where the agent's login AND the person the turn answers to
  (`turn_asker.accountable_person`: its verified asker, the person it was started for, or the schedule's creator) may
  each manage that workspace. An agent with no accountable person behind its turn manages nothing.

Never, whatever else holds: a learning run's own turn (a turn in a conversation stamped `brain_run`, or any credential
or header naming one), so a run cannot widen itself; an OAuth client, a site's or MCP host's delegated token, an
act-as token or a caller token (they speak FOR a person, or for a turn's asker); a guest, a contact, or anyone not an
owner or admin there. A person's own choices (`/api/me/learning/…`), the manager leg and reporting lines are not
management and stay the person's alone (`person.py`). `tests/test_brain_agent_admin.py`."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from apps.api.errors import TYPE_FORBIDDEN, ProblemError
from apps.tokens import kinds
from apps.workspaces import permissions as perms

from . import person as person_mod
from .models import BRAIN_RUN_KEY

#: The turn header a request may name a turn with (`harness.acting_person.TURN_HEADER`).
_TURN_HEADER = "HTTP_X_CANOPY_TURN_ID"


def may_manage(user, ws: str) -> bool:
    """May `user` manage learning at `ws`: a canopy admin, or an owner or admin there (inherited from above)."""
    if not ws or not getattr(user, "is_authenticated", False):
        return False
    return bool(getattr(user, "is_superuser", False)) or perms.can(user, ws, perms.BRAIN_MANAGE)


@dataclass(frozen=True)
class Actor:
    """Who is managing: `user` acts (the person, or the agent's own login) and `person` answers for it (the person
    themselves, or the turn's accountable person). `agent` and `turn` are set for an admin agent."""

    user: Any
    person: Any
    agent: Any = None
    turn: Any = None

    @property
    def is_agent(self) -> bool:
        return self.agent is not None

    def may_manage(self, ws: str) -> bool:
        """Both the one acting and the one answering for it manage learning at `ws`."""
        return may_manage(self.user, ws) and (not self.is_agent or may_manage(self.person, ws))

    def managed(self, slugs) -> set[str]:
        """Of `slugs`, where this actor manages learning (`may_manage`, read once per side: `perms.slugs_with`)."""
        slugs = set(slugs)
        out = _managed(self.user, slugs)
        return out & _managed(self.person, slugs) if self.is_agent else out

    @property
    def label(self) -> str:
        """How a record names who did it: "<email>", or "<agent> for <person's email>"."""
        if not self.is_agent:
            return self.user.email
        return f"{self.agent.slug} for {self.person.email}"

    def record(self) -> dict:
        """Who did it, for an audit row or an event: ids and addresses, never a word."""
        if not self.is_agent:
            return {"by_id": self.user.pk, "by": self.user.email}
        return {"by_id": self.user.pk, "by": self.user.email, "agent": self.agent.slug, "turn": str(self.turn.pk),
                "for_id": self.person.pk, "for": self.person.email}


def _managed(user, slugs: set[str]) -> set[str]:
    if getattr(user, "is_superuser", False):
        return set(slugs)
    return set(perms.slugs_with(user, perms.BRAIN_MANAGE, within=slugs))


def _names_a_run(turn) -> bool:
    """`turn` is a learning run's own turn: its conversation is a run's, or canopy started it as one."""
    from apps.canopy_sessions import access  # noqa: PLC0415 — canopy_sessions imports this app's readers

    if turn is None:
        return False
    if (turn.initiator_via or "") == "brain-run":
        return True
    conversation = access.turn_conversation(turn)
    return conversation is not None and BRAIN_RUN_KEY in (conversation.metadata or {})


def _named_turns(request) -> list:
    """Every turn the request names: by its credential (a caller token's turn, a turn's own login) or a header."""
    from apps.harness.models import Turn  # noqa: PLC0415 — harness imports contacts, which imports this app

    from . import lineage  # noqa: PLC0415 — lineage reads harness

    out = []
    try:
        out.append(lineage.turn_of_request(request))
    except Exception:  # noqa: BLE001 — a credential that names no turn names none
        pass
    named = (getattr(request, "META", None) or {}).get(_TURN_HEADER) or ""
    if named:
        try:
            out.append(Turn.objects.select_related("chat_session").filter(pk=named).first())
        except Exception:  # noqa: BLE001 — not a turn id: names no turn
            pass
    cred = getattr(request, "auth_credential", None) or {}
    if cred.get("turn_id"):
        out.append(Turn.objects.select_related("chat_session").filter(pk=cred["turn_id"]).first())
    return [t for t in out if t is not None]


def resolve(request) -> tuple[Actor | None, str]:
    """The actor behind `request`, or (None, why not). Never names a person in its reason."""
    if any(_names_a_run(t) for t in _named_turns(request)):
        return None, "brain_run"
    why = person_mod.refusal(request)
    if why is None:
        return Actor(user=request.user, person=request.user), ""
    return _agent(request)


def _agent(request) -> tuple[Actor | None, str]:
    from apps.harness import turn_asker  # noqa: PLC0415 — harness imports contacts, which imports this app

    if getattr(request, "contact", None) is not None or getattr(request, "runner_token", None) is not None:
        return None, "not_an_agent"
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return None, "anonymous"
    # Only the login canopy issued for one running turn (a PAT the server holds as a `TurnAgentToken`): an OAuth
    # client, a delegated or act-as token and a caller token speak for a person or a turn's asker, never the agent.
    if kinds.of_request(request) is not kinds.KINDS[kinds.PAT]:
        return None, "credential"
    turn = turn_asker.serving_turn(user)
    agent = turn_asker.turn_agent(turn) if turn is not None else None
    if agent is None or not agent.is_own_login(user):
        return None, "not_in_its_turn"
    if _names_a_run(turn):
        return None, "brain_run"
    accountable = turn_asker.accountable_person(turn)
    if accountable is None:
        return None, "no_accountable_person"
    return Actor(user=user, person=accountable, agent=agent, turn=turn), ""


def of(request) -> Actor:
    """The actor behind `request`, else a 403. A learning run's own turn is told so; anyone else is told nothing."""
    actor, why = resolve(request)
    if actor is None:
        if why == "brain_run":
            raise ProblemError(403, "A learning run may not manage learning", type_=TYPE_FORBIDDEN,
                               detail="a learning run's own turn cannot change what it reads, its budget, its targets "
                                      "or its findings, or run itself")
        raise ProblemError(403, "Only a person as themselves, or an admin agent in its own turn, may do this",
                           type_=TYPE_FORBIDDEN)
    return actor


def note(request, actor: Actor) -> None:
    """Put who acted on the request's settings audit row (`audit.note_acting`)."""
    from apps.events import audit  # noqa: PLC0415 — as this app's other event writers

    audit.note_acting(request, actor.record())


def event(actor: Actor, *, kind: str, summary: str, workspace, payload: dict) -> None:
    """One `brain.manage` event in `workspace` for an act that is not a setting (Run now, a flag), naming who did it."""
    import logging  # noqa: PLC0415 — only a failure pays

    from apps.events import services as events  # noqa: PLC0415 — as above

    try:
        events.record([{"source": "brain.manage", "kind": kind, "summary": f"{actor.label}: {summary}"[:500],
                        "payload": {**payload, **actor.record()}}], workspace=workspace)
    except Exception:  # noqa: BLE001 — a log line never fails the act
        logging.getLogger(__name__).exception("could not record %s", kind)
