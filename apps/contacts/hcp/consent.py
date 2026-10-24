"""The person's own switches — may agents learn about them, and use it — and the
rollout gate that decides who has memory at all."""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..models import Person, PersonFact, PersonGrant
from .core import Actor, audit, denied, malformed
from .sensitive import is_sensitive_session, is_sensitive_turn

# --- the person's own switches: may agents learn about them, and use it? -------------

#: What an agent is told when the switch an operation needs is off. `scope-denied`,
#: the registered type for "outside what you are authorized for" (3.4.4).
RECORD_OFF = "this person has not let agents learn about them"
USE_OFF = "this person has not let agents use what they have learned"
#: Why memory cannot be turned on in a sensitive conversation.
SENSITIVE_OFF = ("this session holds a host's sensitive data, and memory is off in a sensitive conversation: "
                 "agents neither learn from it nor read the person's memory in it")
NOT_GRANTED = ("this person has not granted this agent access for that yet — they are asked "
               "in the session, not by the agent")
#: Which switch an operation needs: "record" for a write, "use" for a read,
#: "any" for one that serves no entry content (an agent listing its own grant).
RECORD, USE, ANY = "record", "use", "any"


FEATURES = ("record", "use")


# --- who has memory at all, for now: the rollout gate ---------------------------------
#
# Jonathan, 2026-10-11: memory is on "for only me right now", and he is the only one who
# sees its UI. `settings.HCP_PEOPLE` lists the canopy addresses it is on for (empty =
# nobody; "*" = everyone, for tests and local dev); `settings.HCP_CONTACT_OPT_IN` decides
# whether a trusted site's visitors are offered it. Outside the rollout every switch
# reads as off (`memory_state`), so no agent reads or records, and every way to turn
# anything on or grant anything is refused with NOT_ROLLED_OUT. Nothing held is touched:
# the person can still see, download and delete it (spec 3.3.5, 3.1), and putting them
# in the rollout later brings back exactly what they had.

NOT_ROLLED_OUT = "memory isn't available for you yet"


def _rollout_people() -> list[str]:
    from django.conf import settings

    return [a.strip().lower() for a in (getattr(settings, "HCP_PEOPLE", None) or []) if a.strip()]


def rollout_allows_address(address: str | None) -> bool:
    people = _rollout_people()
    return "*" in people or bool(address and address.strip().lower() in people)


def rollout_allows(person: Person | None) -> bool:
    """Is memory on at all for this person? A canopy account counts by its own
    address; a person with no canopy account (a site's visitor, a correspondent)
    counts by name in HCP_PEOPLE, or when contacts are let in at all."""
    from django.conf import settings

    if person is None:
        return False
    if person.user_id:
        return rollout_allows_address(person.user.email)
    if person.email and rollout_allows_address(person.email):
        return True
    return bool(getattr(settings, "HCP_CONTACT_OPT_IN", False))


def require_rollout(person: Person | None) -> None:
    """Refuse any act that would turn memory on, widen it, or grant access, for a
    person outside the rollout. Turning things off, revoking, deleting and
    downloading never call this."""
    if not rollout_allows(person):
        raise denied(NOT_ROLLED_OUT)


def has_memory(person: Person | None) -> bool:
    """Does canopy hold anything of this person's memory — an entry or a grant?
    A person outside the rollout who does still sees it (paused), to download or
    delete."""
    if person is None or person.pk is None:
        return False
    return (PersonFact.objects.filter(person=person).exists()
            or PersonGrant.objects.filter(person=person).exists())
#: The audit event types for each setting change, by feature.
_EVENTS = {"record": "agentRecord", "use": "agentUse"}
_SESSION_EVENTS = {"record": "sessionRecord", "use": "sessionUse"}


def session_of(turn):
    """The canopy chat session a turn belongs to, or None (a turn with no session
    uses the person's defaults)."""
    if turn is None or not getattr(turn, "chat_session_id", None):
        return None
    return turn.chat_session


def project_of(turn, workspace=None):
    """The board project a turn is working in — the project of the task it was
    dispatched from — or None. With `workspace`, only a project of that workspace's
    agents (an entry may only be filed against a project where it is written)."""
    task = getattr(turn, "raised_from_task", None) if turn is not None else None
    project = getattr(task, "project", None) if task is not None else None
    if project is None:
        return None
    ws_id = getattr(workspace, "pk", workspace)
    if workspace is not None and project.workspace_id != ws_id:
        return None
    return project


def _override(person: Person, session):
    if session is None:
        return None
    from ..models import SessionAgentMemory

    return SessionAgentMemory.objects.filter(session=session, person=person).first()


def memory_state(person: Person, session=None) -> dict:
    """Per feature: `available`, `default`, this session's `override` (None =
    inherit) and the `effective` value — available AND (override, else default).
    Outside the rollout nothing is available, so nothing is effective; in a
    sensitive conversation nothing is effective either — an agent neither learns
    from it nor reads the person's memory into it (the read's query is the turn's
    own words, which a search keeps)."""
    row = _override(person, session)
    rolled_out = rollout_allows(person)
    sealed = is_sensitive_session(session)
    out = {}
    for f in FEATURES:
        available = rolled_out and bool(getattr(person, f"hcp_{f}_available"))
        default = bool(getattr(person, f"hcp_{f}_default"))
        override = getattr(row, f) if row is not None else None
        out[f] = {"available": available, "default": default, "override": override,
                  "effective": (not sealed) and available and (default if override is None else bool(override))}
    return out


def effective(person: Person, turn=None) -> dict:
    """{record, use} as they apply to the turn being served — both off for a turn of
    a sensitive conversation, a sensitive-data box, or one descended from either."""
    if turn is not None and is_sensitive_turn(turn):
        return {f: False for f in FEATURES}
    state = memory_state(person, session_of(turn))
    return {f: state[f]["effective"] for f in FEATURES}


def may_record(person: Person, turn=None) -> bool:
    return effective(person, turn)["record"]


def may_use(person: Person, turn=None) -> bool:
    return effective(person, turn)["use"]


def require(person: Person, need: str, turn=None) -> None:
    """Raise `scope-denied` unless what `need` requires is on for this turn."""
    require_rollout(person)
    if turn is not None and is_sensitive_turn(turn):
        raise denied(SENSITIVE_OFF)
    eff = effective(person, turn)
    if need == RECORD and not eff["record"]:
        raise denied(RECORD_OFF)
    if need == USE and not eff["use"]:
        raise denied(USE_OFF)
    if need == ANY and not (eff["record"] or eff["use"]):
        raise denied(RECORD_OFF + ", nor use what they have learned")


_DETAILS = {
    "agentRecord.enabled": "Learn from me is available: agents you allow can add to your memory",
    "agentRecord.disabled": "Learn from me is off: no agent adds to your memory; nothing was deleted",
    "agentUse.enabled": "Use my memory is available: agents you allow can read your memory",
    "agentUse.disabled": "Use my memory is off: no agent reads your memory; nothing was deleted",
    "agentRecord.defaultOn": "Learn from me is on in new conversations",
    "agentRecord.defaultOff": "Learn from me is off in new conversations unless you turn it on",
    "agentUse.defaultOn": "Use my memory is on in new conversations",
    "agentUse.defaultOff": "Use my memory is off in new conversations unless you turn it on",
}


def set_agent_memory(person: Person, *, actor: Actor, record: dict | None = None,
                     use: dict | None = None) -> list[str]:
    """Change the person's canopy-level settings, as the person. Each of `record` /
    `use` is `{available?, default?}`; a missing key leaves it alone. Returns the
    audit event types written — one per setting that actually changed. Outside the
    rollout only turning something OFF is accepted."""
    if any(bool((change or {}).get(key)) for change in (record, use)
           for key in ("available", "default")):
        require_rollout(person)
    now, fields, events = timezone.now(), [], []
    for f, change in (("record", record), ("use", use)):
        for key, (on, off) in (("available", ("enabled", "disabled")),
                               ("default", ("defaultOn", "defaultOff"))):
            value = (change or {}).get(key)
            name = f"hcp_{f}_{key}"
            if value is None or bool(getattr(person, name)) == bool(value):
                continue
            setattr(person, name, bool(value))
            fields.append(name)
            stamp = f"hcp_{f}_changed_at"
            setattr(person, stamp, now)
            if stamp not in fields:
                fields.append(stamp)
            events.append(f"{_EVENTS[f]}.{on if value else off}")
    if not fields:
        return []
    with transaction.atomic():
        person.save(update_fields=fields)
        for event in events:
            audit(person, event, actor=actor, detail=_DETAILS[event])
    return events


#: What a session override may be set to.
ON, OFF, INHERIT = "on", "off", "inherit"


def set_session_memory(person: Person, session, *, actor: Actor, record: str | None = None,
                       use: str | None = None) -> list[str]:
    """Set this person's override for one session: "on" / "off" / "inherit" per
    feature (`None` leaves it alone). Setting "on" for a feature the person has not
    made available is refused — a session never widens the canopy level."""
    from ..models import SessionAgentMemory

    changes = {"record": record, "use": use}
    if ON in changes.values():
        require_rollout(person)
        if is_sensitive_session(session):
            raise denied(SENSITIVE_OFF)
    for f, value in changes.items():
        if value not in (None, ON, OFF, INHERIT):
            raise malformed(f"{f} is on, off or inherit")
        if value == ON and not getattr(person, f"hcp_{f}_available"):
            raise denied(f"{f} is not available for you; make it available first")
    events = []
    with transaction.atomic():
        row, _ = SessionAgentMemory.objects.select_for_update().get_or_create(
            session=session, person=person)
        fields = []
        for f, value in changes.items():
            if value is None:
                continue
            new = None if value == INHERIT else (value == ON)
            if getattr(row, f) == new:
                continue
            setattr(row, f, new)
            fields.append(f)
            events.append(f"{_SESSION_EVENTS[f]}.{value}")
        if fields:
            row.save(update_fields=fields + ["updated_at"])
            for event in events:
                audit(person, event, actor=actor, detail=f"session {session.pk}")
    return events
