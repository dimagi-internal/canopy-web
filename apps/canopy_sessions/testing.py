"""Test-support helpers for chat authorship. Imported by tests only; nothing in
the running app imports this module.

canopy does not write the authorship marker (attribution is server-side, see
`authorship.py`'s module docstring). Rows recorded earlier, and any transcript
backfilled from that period, still carry the marker, so `authorship.parse` keeps
reading it forever. Tests that exercise that back-compat path need a way to build
a realistic marked string without a production function — this is that builder,
kept here rather than copy-pasted per test module.
"""
from __future__ import annotations

import uuid


def _escape(name: str) -> str:
    one_line = " ".join(name.split())
    return one_line.replace("\\", "\\\\").replace('"', '\\"')


def legacy_marker(text: str, *, name: str, turn_id, user_id: int | None = None,
                   contact_id: int | None = None) -> str:
    """The exact marker string a legacy chat-send prompt carried."""
    if (user_id is None) == (contact_id is None):
        raise ValueError("exactly one of user_id / contact_id")
    who = f"user={int(user_id)}" if user_id is not None else f"contact={int(contact_id)}"
    tid = uuid.UUID(str(turn_id)).hex
    return f'[canopy from="{_escape(name)}" {who} turn={tid}]\n{text}'
