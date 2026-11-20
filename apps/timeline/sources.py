"""Registry + merge for the team activity timeline.

Each entry points at a ``recent_events(*, limit, before, user, workspace_slugs)`` callable living
in a subsystem's own ``timeline.py``. The aggregator calls the enabled sources,
merges their events, sorts newest-first, and slices to ``limit``. A source that
raises is logged and skipped — one broken subsystem never blanks the whole feed.

No new tables: every source reads live models at request time. Each source
returns at most ``limit`` events (and only events older than ``before`` when a
cursor is given), so worst-case work is ``len(sources) * limit`` rows.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from importlib import import_module

from .types import ActivityEvent

logger = logging.getLogger(__name__)

#: (key, label, "module.path", "callable_name"). Order is the rail order.
_REGISTRY: list[tuple[str, str, str, str]] = [
    ("ddd", "DDD", "apps.runs.timeline", "recent_events"),
    ("walkthroughs", "Walkthroughs", "apps.walkthroughs.timeline", "recent_events"),
    ("shareouts", "Shareouts", "apps.shareouts.timeline", "recent_events"),
    ("agents", "Agents", "apps.agents.timeline", "recent_events"),
    ("sessions", "Sessions", "apps.session_sharing.timeline", "recent_events"),
]


def subsystem_catalog() -> list[dict[str, str]]:
    """[{key, label}] for the filter rail, in registry order."""
    return [{"key": key, "label": label} for key, label, _, _ in _REGISTRY]


def valid_subsystem(key: str | None) -> str | None:
    """Return ``key`` if it's a known subsystem, else ``None`` (→ all)."""
    known = {k for k, _, _, _ in _REGISTRY}
    return key if key in known else None


def _resolve(module_path: str, attr: str) -> Callable[..., list[ActivityEvent]]:
    return getattr(import_module(module_path), attr)


def _call_source(fn, *, limit, before, user, workspace_slugs) -> list[ActivityEvent]:
    """Invoke a source with the caller's tenant scope.

    Every source takes ``workspace_slugs`` and filters every query by it: the
    aggregator (framework) offers the scope without importing the source's models,
    so the framework→product boundary holds. A source that leaves it out is a
    feed of every tenant's rows, so it is called with the scope anyway and the
    TypeError drops it (logged) rather than reading unscoped."""
    return fn(limit=limit, before=before, user=user, workspace_slugs=workspace_slugs) or []


def gather(
    *,
    subsystem: str | None,
    limit: int,
    before: tuple[dt.datetime, str] | None,
    user,
    workspace_slugs: set[str],
) -> list[ActivityEvent]:
    """Merged, newest-first events across the enabled sources, capped at ``limit``.

    ``before`` is a compound ``(at, id)`` cursor. Sources fetch inclusively
    (``at <= before_at``) so events whose timestamps tie at the cursor aren't
    lost; the aggregator then applies the exact ``(at, id)`` tuple bound. This
    keeps "show more" from silently skipping events that share a timestamp (e.g.
    two day-aligned shareouts both stamped 23:59:59).

    ``workspace_slugs`` scopes the feed to the caller's tenants; every source
    filters by it (see :func:`_call_source`).
    """
    before_at = before[0] if before else None
    entries = [e for e in _REGISTRY if subsystem is None or e[0] == subsystem]
    events: list[ActivityEvent] = []
    for key, _label, module_path, attr in entries:
        try:
            fn = _resolve(module_path, attr)
            events.extend(
                _call_source(
                    fn,
                    limit=limit,
                    before=before_at,
                    user=user,
                    workspace_slugs=workspace_slugs,
                )
            )
        except Exception:  # one bad source must not blank the feed
            logger.exception("timeline source %r failed", key)
    # Total order with id as the tiebreak so the compound cursor is exact.
    events.sort(key=lambda ev: (ev.at, ev.id), reverse=True)
    if before is not None:
        events = [ev for ev in events if (ev.at, ev.id) < before]
    return events[:limit]
