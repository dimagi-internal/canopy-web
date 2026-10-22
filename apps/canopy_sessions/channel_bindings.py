"""Which outside channel thread a session is connected to — supplied by the channel.

A session row (`api._out`) carries, per channel, where its conversation is also
being had: a Slack thread today. canopy_sessions does not know any channel; each
one registers a reader from its own `AppConfig.ready` (`apps/slack/apps.py`), as
`apps.agents.move` takes its participants. A reader gets the session and answers
the channel's binding dict, or None; it must be cheap (it runs on every list row)
and must not call out to the channel.
"""
from __future__ import annotations

from collections.abc import Callable

_READERS: dict[str, Callable] = {}


def register(key: str, reader: Callable) -> None:
    """`reader(session) -> dict | None`, answered under `key` on every session row."""
    _READERS[key] = reader


def of(session) -> dict:
    """Every registered channel's binding for `session`, keyed by channel."""
    return {key: reader(session) for key, reader in _READERS.items()}
