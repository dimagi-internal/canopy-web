"""Timeline source for the projects app: ``projects`` (context pushes + skill
actions).

``ProjectContext`` rows with ``context_type="insight"`` are excluded: they were
the retired Insights feed's cards (removed 2026-10), and the rows that remain are
unreachable on purpose rather than resurfaced here.

Each returns *candidates* (newest ``limit`` per component plus cursor-instant
ties); :func:`apps.timeline.sources.gather` does the final merge/order/slice.
"""
from __future__ import annotations

import datetime as dt

from .models import ProjectAction, ProjectContext


def project_events(
    *, limit: int, before: dt.datetime | None, user, workspace_slugs=None
) -> list:
    from apps.timeline.types import ActivityEvent, cursor_page, first_line

    events: list[ActivityEvent] = []

    def _scope(qs):
        # Context/action rows inherit tenancy via their project's workspace.
        return qs if workspace_slugs is None else qs.filter(project__workspace_id__in=workspace_slugs)

    ctx = _scope(
        ProjectContext.objects.exclude(context_type="insight").select_related("project")
    ).order_by("-created_at")
    for c in cursor_page(ctx, "created_at", before=before, limit=limit):
        label = dict(ProjectContext.CONTEXT_TYPES).get(c.context_type, c.context_type)
        events.append(
            ActivityEvent(
                subsystem="projects",
                kind="context",
                at=c.created_at,
                title=f"{label}: {first_line(c.content) or '—'}",
                project_slug=c.project.slug,
                actor=c.source or None,
                href="/",
                id=f"context:{c.id}",
                icon="note",
            )
        )

    acts = _scope(ProjectAction.objects.select_related("project")).order_by("-started_at")
    for a in cursor_page(acts, "started_at", before=before, limit=limit):
        events.append(
            ActivityEvent(
                subsystem="projects",
                kind="action",
                at=a.started_at,
                title=f"{a.skill_name} · {a.status}",
                project_slug=a.project.slug,
                href="/",
                id=f"action:{a.id}",
                icon="skill",
            )
        )

    return events
