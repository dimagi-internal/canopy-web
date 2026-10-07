"""Timeline source for the agents subsystem.

Two ``kind``s merge under ``agents``: ``sync`` (a periodic manager sync) and
``task`` (a task as it first appears).

Returns *candidates* (newest ``limit`` per component plus cursor-instant ties);
:func:`apps.timeline.sources.gather` does the final merge/order/slice.
"""
from __future__ import annotations

import datetime as dt

from .models import AgentSync, AgentTask


def recent_events(
    *, limit: int, before: dt.datetime | None, user, workspace_slugs=None
) -> list:
    from apps.timeline.types import ActivityEvent, cursor_page, truncate

    events: list[ActivityEvent] = []

    def _scope(qs):
        # Agent sub-objects inherit tenancy via their agent's workspace.
        return qs if workspace_slugs is None else qs.filter(agent__workspace_id__in=workspace_slugs)

    syncs = _scope(AgentSync.objects.select_related("agent")).order_by("-period_end")
    for s in cursor_page(syncs, "period_end", before=before, limit=limit):
        events.append(
            ActivityEvent(
                subsystem="agents",
                kind="sync",
                at=s.period_end,
                title=f"{s.agent.name}: {s.title}",
                summary=truncate(s.summary),
                href=f"/agents/{s.agent.slug}/syncs",
                id=f"sync:{s.id}",
                icon="sync",
            )
        )

    tasks = _scope(AgentTask.objects.select_related("agent")).order_by("-created_at")
    for t in cursor_page(tasks, "created_at", before=before, limit=limit):
        events.append(
            ActivityEvent(
                subsystem="agents",
                kind="task",
                at=t.created_at,
                title=f"{t.agent.name}: {t.title}",
                summary=t.get_status_display(),
                href=f"/agents/{t.agent.slug}/tasks",
                id=f"task:{t.id}",
                icon="task",
            )
        )

    return events
