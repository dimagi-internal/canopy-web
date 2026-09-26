"""Helpers for tests that need an agent in a particular state."""
from __future__ import annotations


def admit_contacts(agent) -> None:
    """Publish `full: [contact]` on `agent`, for a test about something OTHER
    than access (email grading, turn modes, widget runs). Since 2026-09-26 an
    agent with no interface refuses contacts, so such a test has to say, in
    the open, that this agent lets them in."""
    from apps.agents.interface import parse

    agent.interface = parse({"full": ["contact"]})
    agent.save(update_fields=["interface"])
