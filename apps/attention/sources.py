"""Where attention items come from — a string registry, like the timeline's.

Each entry names an `items(request, *, slugs)` callable in the app that owns
the thing. Resolved by dotted path so a product app can add a source without
this framework app importing it. A source that raises is logged and skipped:
one broken subsystem never blanks someone's Home.
"""
from __future__ import annotations

import logging
from importlib import import_module

from .types import AttentionRow

logger = logging.getLogger(__name__)

#: (kind, "module.path"). Each module exposes `items(request, *, slugs)`.
REGISTRY: list[tuple[str, str]] = [
    ("task", "apps.agents.attention"),
    ("session", "apps.canopy_sessions.attention"),
    ("access_request", "apps.workspaces.attention"),
    ("join_request", "apps.canopy_sessions.join_requests"),
]


def gather(request, *, slugs: set[str]) -> list[AttentionRow]:
    out: list[AttentionRow] = []
    for kind, path in REGISTRY:
        try:
            out.extend(import_module(path).items(request, slugs=slugs))
        except Exception:  # noqa: BLE001 — one source must not blank the list
            logger.exception("attention source %s failed", kind)
    return out
