"""Helpers for tests that need an agent in a particular state."""
from __future__ import annotations

from apps.agents.models import AgentGrant


def admit_contacts(agent) -> None:
    """Let every known contact use `agent` as an editor, for a test about something OTHER
    than access (email grading, turn modes, widget runs). An agent that lists nobody refuses
    contacts, so such a test has to say, in the open, that this agent lets them in: one line
    on its access list."""

    AgentGrant.objects.update_or_create(
        agent=agent, subject_key=AgentGrant.key_of(AgentGrant.CONTACT),
        defaults=dict(kind=AgentGrant.CONTACT, role="editor", verified=False))
