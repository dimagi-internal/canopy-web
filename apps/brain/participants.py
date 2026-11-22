"""Who is in a source, and why a source is sealed: both read from what canopy already knows.

Participants are filled ONLY from existing rows: the session's creator, its `SessionParticipant`
rows, everyone who wrote a line in it (`Message.author`) or started a turn in it
(`Turn.initiator_user` / `initiator_contact`), including people since taken out of it (as
`canopy_sessions.access.others_in_it` counts them), and the owner of the box a runner-discovered
session ran on.
Agents are never participants: an agent's own login is not a person who agreed to anything.
A contact is one, and makes a source ineligible for every shared use."""
from __future__ import annotations

from django.db.models import Q

from apps.agents.models import Agent
from apps.canopy_sessions.models import SessionParticipant
from apps.harness import runner_requirements as rr
from apps.harness import sensitivity
from apps.harness.models import Turn

from .models import BRAIN_RUN_KEY, BrainSource, SourceParticipant


def is_agent_login(user) -> bool:
    """Is `user` (a user or a user id) an agent's own canopy login: never a person, never a participant."""
    pk = getattr(user, "pk", user)
    return pk is not None and Agent.objects.filter(user_id=pk).exists()


def sealed_reason(session) -> str:
    """Why this session is shut to every learning use, or "". Read live, never cached, and the
    sensitive test is `access._not_sensitive_q`'s: ANY runner requirement at all is sensitive
    (a new flag fails closed). `tests/test_brain_sources.py` holds the two together. A brain run's own
    conversation is sealed too: it is learning's output, never its input."""
    if session is None:
        return BrainSource.SEALED_DELETED
    if rr.METADATA_KEY in (session.metadata or {}):
        return BrainSource.SEALED_SENSITIVE
    if session.contact_id is not None:
        return BrainSource.SEALED_CONTACT
    if session.content_deleted_at is not None:
        return BrainSource.SEALED_DELETED
    if BRAIN_RUN_KEY in (session.metadata or {}):
        # A learning run's conversation retells the sessions it read; its only "participant" is the admin its
        # turn answers to. Learning from it would launder what those sessions allowed into another class.
        return BrainSource.SEALED_BRAIN_RUN
    return ""


def people_of(session) -> list[tuple[str, int | None, int | None]]:
    """`(how, user_id, contact_id)` for each person whose words are in `session`, once each."""
    found: dict[tuple, str] = {}
    if session.created_by_id is not None:
        found[("u", session.created_by_id)] = SourceParticipant.CREATOR
    for uid in SessionParticipant.objects.filter(session=session).values_list("user_id", flat=True):
        found.setdefault(("u", uid), SourceParticipant.PARTICIPANT)
    for kind, pk in spoke_in(session):           # their words stay in it after they are taken out
        found.setdefault((kind, pk), SourceParticipant.PARTICIPANT)
    if session.contact_id is not None:
        found[("c", session.contact_id)] = SourceParticipant.PARTICIPANT
    if session.created_by_id is None and session.agent_id is None:
        binding = getattr(session, "runner_binding", None)
        runner = getattr(binding, "runner", None)
        if runner is not None and runner.owner_id is not None:
            found.setdefault(("u", runner.owner_id), SourceParticipant.BOX_OWNER)
    agent_logins = set(Agent.objects.filter(user_id__in=[k[1] for k in found if k[0] == "u"])
                       .values_list("user_id", flat=True))
    return [(how, key[1] if key[0] == "u" else None, key[1] if key[0] == "c" else None)
            for key, how in found.items() if not (key[0] == "u" and key[1] in agent_logins)]


def spoke_in(session) -> set[tuple[str, int]]:
    """`("u", user_id)` / `("c", contact_id)` for everyone who wrote a line in `session` or started a
    turn in it, whether or not they are still in it."""
    out: set[tuple[str, int]] = set()
    for author in (session.messages.filter(author__isnull=False).values_list("author", flat=True).distinct()):
        if not isinstance(author, dict):
            continue
        for key, kind in (("user_id", "u"), ("contact_id", "c")):
            try:
                pk = int(author.get(key)) if author.get(key) is not None else None
            except (TypeError, ValueError):
                pk = None
            if pk is not None:
                out.add((kind, pk))
    for uid, cid in (Turn.objects.filter(sensitivity.turns_of_q(session.pk))
                     .filter(Q(initiator_user__isnull=False) | Q(initiator_contact__isnull=False))
                     .values_list("initiator_user_id", "initiator_contact_id").distinct()):
        if uid is not None:
            out.add(("u", uid))
        if cid is not None:
            out.add(("c", cid))
    return out
