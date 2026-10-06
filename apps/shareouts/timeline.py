"""Timeline source for shareouts (teammate-facing work briefings)."""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote

from .models import Shareout


def _ymd(d: dt.datetime) -> str:
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def _period_slug(start: dt.datetime, end: dt.datetime) -> str:
    """Mirror the frontend ``periodSlug`` (ShareoutsPage) so the permalink resolves.

    Day-aligned windows (00:00:00→23:59:59 UTC) slug as a date or ``start_end``;
    a precise mid-day run slugs as ``YYYY-MM-DD-HHMM``. Built from UTC so it's
    identical for every viewer.
    """
    s = start.astimezone(dt.UTC)
    e = end.astimezone(dt.UTC)
    day_aligned = (
        s.hour == 0
        and s.minute == 0
        and s.second == 0
        and e.hour == 23
        and e.minute == 59
        and e.second == 59
    )
    if day_aligned:
        return _ymd(s) if _ymd(s) == _ymd(e) else f"{_ymd(s)}_{_ymd(e)}"
    return f"{_ymd(s)}-{s.hour:02d}{s.minute:02d}"


def recent_events(
    *, limit: int, before: dt.datetime | None, user, workspace_slugs=None
) -> list:
    from apps.timeline.types import ActivityEvent, cursor_page, truncate

    qs = Shareout.objects.order_by("-period_end")
    if workspace_slugs is not None:
        qs = qs.filter(workspace_id__in=workspace_slugs)
    return [
        ActivityEvent(
            subsystem="shareouts",
            kind="shareout",
            at=s.period_end,
            title=s.title,
            summary=truncate(s.summary),
            project_slug=s.project_slug or None,
            actor=s.author or s.source or None,
            href=f"/shareouts/{quote(_period_slug(s.period_start, s.period_end))}",
            id=f"shareout:{s.id}",
            icon="doc",
        )
        for s in cursor_page(qs, "period_end", before=before, limit=limit)
    ]
