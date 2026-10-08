"""Timeline source for standalone walkthrough uploads.

Walkthroughs tied to a DDD run (they carry a ``run_id``/``narrative_slug``) are
surfaced by the ``ddd`` source instead — this emits only one-off uploads so the
two subsystems don't double-count the same artifact.
"""
from __future__ import annotations

import datetime as dt

from django.db.models import Q

from .models import Walkthrough
from apps.workspaces import services as wsvc


def recent_events(
    *, limit: int, before: dt.datetime | None, user, workspace_slugs=None
) -> list:
    from apps.timeline.types import ActivityEvent, actor_name, cursor_page, truncate

    qs = (
        Walkthrough.objects.filter(Q(run_id__isnull=True) | Q(run_id=""))
        .filter(Q(narrative_slug__isnull=True) | Q(narrative_slug=""))
        # A walkthrough with no workspace has no address to link to.
        .filter(workspace__isnull=False)
        .select_related("owner")
        .order_by("-created_at")
    )
    if workspace_slugs is not None:
        qs = qs.filter(workspace_id__in=workspace_slugs)
    return [
        ActivityEvent(
            subsystem="walkthroughs",
            kind="walkthrough",
            at=w.created_at,
            title=w.title,
            summary=truncate(w.description),
            project_slug=w.project_slug,
            actor=actor_name(w.owner),
            href=wsvc.scoped_path(w.workspace_id, f"/walkthrough/{w.id}"),
            id=f"walkthrough:{w.id}",
            icon="video" if w.kind == Walkthrough.KIND_VIDEO else "deck",
        )
        for w in cursor_page(qs, "created_at", before=before, limit=limit)
    ]
