"""The default interface, apart from `interface.py` so the Agent model can use it as a
field default without importing the whole access machinery.

Every agent has an interface from the moment it exists: this one, until its owner
changes it. See `apps/agents/interface.py` and `docs/architecture/access.md`.
"""
from __future__ import annotations

VERSION = 1


def default_interface() -> dict:
    """No `full:` rule, no capability, `callers_default: none` — the owner, the admins and
    the workspace's editors reach the agent, and nobody else. A fresh dict every call."""
    return {"version": VERSION, "full": [], "capabilities": {}, "callers_default": "none"}
