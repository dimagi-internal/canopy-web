"""Django Ninja router for /api/events — record and read.

Deliberately thin over ``services`` so a later MCP tool shares one
implementation.

There is no mutation route and no ``resolve``. ``feedback`` has one because a
disposition is a real decision someone took; an event is a record of something
that happened, and a log you can edit is not a record. If a fault needs a
decision attached, that decision belongs on the thing the turn produced — not
on the observation.
"""
from __future__ import annotations

import datetime as dt

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.events import services
from apps.events.models import Event
from apps.events.schemas import EventBatchIn, EventListOut, EventRecordOut
from apps.workspaces import services as wsvc

router = Router(auth=session_auth, tags=["events"])


def _out(ev: Event) -> dict:
    return {
        "id": ev.pk,
        "workspace": ev.workspace_id,
        "source": ev.source,
        "kind": ev.kind,
        "level": ev.level,
        "key": ev.key,
        "summary": ev.summary,
        "payload": ev.payload or {},
        "count": ev.count,
        "first_seen_at": ev.first_seen_at.isoformat(),
        "last_seen_at": ev.last_seen_at.isoformat(),
    }


@router.post("/", response=EventRecordOut, summary="Record events (batch, coalescing)")
def record_events(request: HttpRequest, payload: EventBatchIn) -> dict:
    """Repeats of ``(workspace, source, key)`` coalesce onto one row with a
    count, so a permanently-stuck retry loop stays one row instead of one row
    per tick. A blank key never coalesces."""
    pinned = getattr(request, "workspace_slug", None)
    home = (
        wsvc.Workspace.objects.filter(slug=pinned).first() if pinned else None
    ) or wsvc.user_default_workspace(request.user)
    if home is None:
        # Only reachable for an authenticated user who belongs to nothing.
        # Fail with a real message rather than an IntegrityError on the FK.
        raise HttpError(422, "no workspace available to record this event in")
    return services.record([item.model_dump() for item in payload.items], workspace=home)


@router.get("/", response=EventListOut, summary="List events")
def list_events(
    request: HttpRequest,
    source: str | None = None,
    kind: str | None = None,
    level: str | None = None,
    since_minutes: int | None = None,
    limit: int = 100,
) -> dict:
    """Newest-touched first. ``source`` and ``kind`` are prefix matches, so
    ``?source=runner`` covers every runner subsystem without the caller
    knowing the full dotted names."""
    since = None
    if since_minutes:
        since = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=max(1, since_minutes))
    qs = services.list_events(
        workspace_slugs=wsvc.user_workspace_slugs(request.user),
        source=source,
        kind=kind,
        level=level,
        since=since,
        limit=limit,
    )
    return {"items": [_out(ev) for ev in qs]}
