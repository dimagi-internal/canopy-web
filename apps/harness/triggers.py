"""Agent triggers: what may fire, on whose authority, and which mail is whose.

A trigger is an `AgentSchedule` row (the model keeps its historical name; see its
docstring): `cron` fires on a cadence (`schedule_turns`), `inbound_mail` fires when
aligned mail from its bound sender reaches the agent (`channels/email.arrive`).
docs/superpowers/specs/2026-10-27-agent-triggers-design.md.

**Authority is the creator's, never more.** A trigger has no standing of its own:
its turn is a SYSTEM turn accountable to the person who set it up, so the access
rule and the turn-mode ladder bound it by them (`turn_mode._accountable_cap`,
`turn_mode.trigger_posture`). And a trigger whose creator no longer holds the right
to set it up does not fire at all — `refusal` says why, every firing path asks it,
and `alerts_trigger` raises an alert for each one, so it never stops silently.
"""
from __future__ import annotations

import logging
import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import initiator as who
from .models import AgentSchedule

logger = logging.getLogger(__name__)

CRON, INBOUND_MAIL = AgentSchedule.CRON, AgentSchedule.INBOUND_MAIL
KINDS = (CRON, INBOUND_MAIL)
#: A trigger's own turn mode. "" = the agent's routing decides.
TURN_MODES = ("", "manual", "auto")
#: `Turn.initiator_via` of a mail trigger's turn; a cron trigger's stays `schedule:<id>`.
VIA = "trigger"


class TriggerRefused(Exception):
    """The trigger may not fire: its creator no longer holds the right to it."""

    def __init__(self, trigger: AgentSchedule, reason: str) -> None:
        super().__init__(reason)
        self.trigger = trigger
        self.reason = reason


class InvalidTrigger(ValueError):
    """A trigger's fields do not describe one that can fire."""


# --- who it answers to -----------------------------------------------------------


def accountable(trigger: AgentSchedule):
    """The person whose authority the trigger fires with: its creator, or None."""
    return trigger.created_by if trigger.created_by_id is not None else None


def may_set_up(user, agent, *, kind: str, turn_mode: str = "") -> str | None:
    """None when `user` may create or change a trigger of `kind` (and `turn_mode`)
    on `agent`, else the role it needs. The same rule `refusal` applies to a
    trigger's creator later, so what may be set up is exactly what may fire.

    * cron: `agent.work` (an editor; its turns are bounded by them anyway);
    * inbound_mail: an agent admin — it admits an outside sender to make the agent
      work, the tier of `SenderTrust` and the agent's interface;
    * turn_mode auto: an agent admin, as for a dispatch that asks for auto.

    The agent's own login holds its workspace role like anyone; `refusal` alone
    lets it stand for itself on a cron trigger it already set up."""
    if kind == INBOUND_MAIL or turn_mode == "auto":
        return None if agent.is_admin(user) else "agent admin"
    if not perms.can(user, agent.workspace_id, perms.AGENT_WORK):
        return perms.MINIMUM_ROLE[perms.AGENT_WORK]
    return None


def refusal(trigger: AgentSchedule) -> str | None:
    """Why `trigger` may not fire now, or None. Asked at every fire and by the
    `trigger_stopped` alert source."""
    agent = trigger.agent
    person = accountable(trigger)
    if person is None:
        # A cron row from before attribution existed fires as canopy's own, as it
        # always has (its turns keep the SYSTEM posture, bounded by nobody); an
        # inbound_mail trigger always records a creator, and without one it is shut.
        if trigger.kind == CRON:
            return None
        return "nobody is recorded as having set it up"
    if not person.is_active:
        return f"{person.email}, who set it up, has been deactivated"
    if agent.is_own_login(person) and trigger.kind == CRON and trigger.turn_mode != "auto":
        return None
    if not wsvc.is_member(person, agent.workspace_id):
        return f"{person.email}, who set it up, is no longer a member of {agent.workspace_id}"
    needed = may_set_up(person, agent, kind=trigger.kind, turn_mode=trigger.turn_mode)
    if needed == "agent admin":
        return f"{person.email}, who set it up, is no longer an admin of {agent.slug}"
    if needed is not None:
        return f"{person.email}, who set it up, may no longer direct {agent.slug}'s work"
    return None


# --- validation ------------------------------------------------------------------


def normalize_address(address: str) -> str:
    address = (address or "").strip().lower()
    try:
        validate_email(address)
    except ValidationError:
        raise InvalidTrigger(f"{address!r} is not an email address") from None
    return address


def validate_pattern(pattern: str) -> str:
    pattern = pattern or ""
    if len(pattern) > 300:
        raise InvalidTrigger("subject_pattern is longer than 300 characters")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise InvalidTrigger(f"subject_pattern is not a valid regex: {exc}") from None
    return pattern


# --- inbound mail ----------------------------------------------------------------


def for_inbound(agent, address: str, subject: str = "") -> AgentSchedule | None:
    """The ONE enabled `inbound_mail` trigger of `agent` bound to `address` whose
    subject pattern matches `subject`, or None.

    Two triggers matching one message is ambiguous and resolves to None (the sender
    stays a contact): a guess would fire one creator's trigger on the other's mail.
    The caller is responsible for the message being aligned; this only answers
    "whose binding is it"."""
    address = (address or "").strip().lower()
    if not address or agent is None:
        return None
    matches = []
    for trigger in (AgentSchedule.objects.select_related("agent", "created_by")
                    .filter(agent=agent, kind=INBOUND_MAIL, enabled=True,
                            sender_address__iexact=address)):
        pattern = trigger.subject_pattern
        try:
            if pattern and not re.search(pattern, subject or ""):
                continue
        except re.error:
            # Validated at write time; a bad pattern in the DB matches nothing.
            continue
        matches.append(trigger)
    if len(matches) > 1:
        logger.warning("mail from %s matches %d triggers of %s; treated as a contact",
                       address, len(matches), agent.slug)
        return None
    return matches[0] if matches else None


def initiator(trigger: AgentSchedule, *, contact=None):
    """A mail trigger's turn initiator: canopy, accountable to the creator, with the
    sender's contact along so its profile still reaches the agent."""
    user = accountable(trigger)
    return who.Initiator(who.SYSTEM, f"{VIA}:{trigger.pk}", who.INTERNAL,
                         user=user if getattr(user, "is_authenticated", False) else None,
                         contact=contact)


def of_turn(turn) -> AgentSchedule | None:
    """The trigger a turn was fired by — `origin_ref.trigger_id` (mail) or
    `schedule_id` (cron) — or None."""
    ref = turn.origin_ref if isinstance(turn.origin_ref, dict) else {}
    pk = ref.get("trigger_id") or ref.get("schedule_id")
    if not isinstance(pk, int):
        return None
    return AgentSchedule.objects.select_related("agent", "created_by").filter(pk=pk).first()


def describe(turn) -> dict | None:
    """What the API and the caller envelope say about the trigger behind a turn:
    the agent reads an automated trigger set up by someone, not a person asking.

    From the turn alone — no query, so a turn listing stays one: the trigger's id
    and name are stamped into `origin_ref` when it fires, and the accountable
    person is the turn's own `initiator_user`."""
    ref = turn.origin_ref if isinstance(turn.origin_ref, dict) else {}
    if ref.get("trigger_id"):
        pk, name, kind = ref["trigger_id"], ref.get("trigger_name", ""), INBOUND_MAIL
    elif ref.get("schedule_id"):
        pk, name, kind = ref["schedule_id"], ref.get("schedule_name", ""), CRON
    else:
        return None
    user = getattr(turn, "initiator_user", None)
    return {"id": pk, "name": name, "kind": kind, "set_up_by": getattr(user, "email", None)}
