"""Who is in a source, and why a source is sealed: both read from what canopy already knows.

Participants are filled ONLY from existing rows (the session's creator, its
`SessionParticipant` rows, the owner of the box a runner-discovered session ran on).
Agents are never participants: an agent's own login is not a person who agreed to anything.
A contact is one, and makes a source ineligible for every shared use."""
from __future__ import annotations

from apps.agents.models import Agent
from apps.harness import runner_requirements as rr

from .models import BrainSource, SourceParticipant


def sealed_reason(session) -> str:
    """Why this session is shut to every learning use, or "". Read live, never cached, and the
    sensitive test is `access._not_sensitive_q`'s: ANY runner requirement at all is sensitive
    (a new flag fails closed). `tests/test_brain_sources.py` holds the two together."""
    if session is None:
        return BrainSource.SEALED_DELETED
    if rr.METADATA_KEY in (session.metadata or {}):
        return BrainSource.SEALED_SENSITIVE
    if session.contact_id is not None:
        return BrainSource.SEALED_CONTACT
    if session.content_deleted_at is not None:
        return BrainSource.SEALED_DELETED
    return ""


def people_of(session) -> list[tuple[str, int | None, int | None]]:
    """`(how, user_id, contact_id)` for each person whose words are in `session`, once each."""
    from apps.canopy_sessions.models import SessionParticipant  # noqa: PLC0415 — as the other lazy imports here

    found: dict[tuple, str] = {}
    if session.created_by_id is not None:
        found[("u", session.created_by_id)] = SourceParticipant.CREATOR
    for uid in SessionParticipant.objects.filter(session=session).values_list("user_id", flat=True):
        found.setdefault(("u", uid), SourceParticipant.PARTICIPANT)
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
