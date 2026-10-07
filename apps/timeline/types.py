"""The common event shape every timeline source emits.

A subsystem "source" (``apps/<app>/timeline.py``) queries its own models and
maps each user-visible happening to an :class:`ActivityEvent`. The aggregator
(:mod:`apps.timeline.sources`) merges events from every source, sorts by ``at``
descending, and slices to the page size — so sources never need to know about
each other.
"""
from __future__ import annotations

import dataclasses
import datetime as dt


@dataclasses.dataclass(frozen=True)
class ActivityEvent:
    """One row on the timeline.

    ``subsystem`` is the filter key (also a UI chip); ``kind`` is the verb within
    that subsystem. ``id`` is ``"<kind>:<pk>"`` — unique per event, used for
    React keys and as the seed for a future canonical object address.
    ``href`` is the real in-app URL to open (``external=True`` for off-site URLs
    like a project link, which open in a new tab).
    """

    subsystem: str
    kind: str
    at: dt.datetime
    title: str
    href: str
    id: str
    summary: str | None = None
    project_slug: str | None = None
    actor: str | None = None
    external: bool = False
    icon: str | None = None


# --- small mapping helpers shared by sources ---------------------------------


def first_line(text: str | None, *, limit: int = 140) -> str:
    """First non-empty line of ``text``, trimmed to ``limit`` chars."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def truncate(text: str | None, *, limit: int = 160) -> str | None:
    """A single trimmed line, or ``None`` when empty (so the field is omitted)."""
    s = (text or "").strip().replace("\n", " ")
    return s[:limit] or None


def cursor_page(qs, ts_field: str, *, before, limit: int) -> list:
    """Candidate rows for one timeline page from a single-timestamp queryset.

    ``qs`` must already be ordered newest-first on ``ts_field``. Returns the
    newest ``limit`` rows at-or-older than the cursor, **plus every row tied at
    the page's boundary timestamp**. The boundary ties matter because SQL's own
    tiebreak need not match the aggregator's global ``(at, id)`` order — without
    them, the globally-newest of a tied set can be sliced off before
    :func:`apps.timeline.sources.gather` re-sorts, and then the cursor skips it
    forever (e.g. many projects' day-aligned shareouts all stamped ``23:59:59``).
    The aggregator applies the exact ``(at, id)`` cursor and the final slice, so a
    source returns candidates, not a finished page.
    """
    if before is not None:
        qs = qs.filter(**{f"{ts_field}__lte": before})
    rows = list(qs[:limit])
    if rows:
        boundary = getattr(rows[-1], ts_field)
        seen = {r.pk for r in rows}
        rows += [r for r in qs.filter(**{ts_field: boundary}) if r.pk not in seen]
    return rows


def actor_name(user) -> str | None:
    """Human label for an owner FK (may be ``None``)."""
    if user is None:
        return None
    return (
        getattr(user, "email", "")
        or getattr(user, "get_full_name", lambda: "")()
        or getattr(user, "username", "")
        or None
    )
