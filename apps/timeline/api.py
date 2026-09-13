"""GET /api/timeline/ — one merged, filterable, team-wide activity feed.

Read-only aggregator over every subsystem (see :mod:`apps.timeline.sources`).
Session-authed like the rest of /api/; since "private" means "dimagi-only" (the
whole app is OAuth-gated), every authenticated user sees the full team feed —
no owner filtering.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.pagination import clamp_limit
from apps.workspaces import services as wsvc

from . import sources
from .schemas import ActivityEventOut, SubsystemOut, TimelineOut
from .types import ActivityEvent

router = Router(auth=session_auth, tags=["timeline"])

_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50


def _encode_cursor(event: ActivityEvent) -> str:
    """Opaque ``"<iso8601>|<id>"`` cursor — a compound (at, id) keyset."""
    return f"{event.at.isoformat()}|{event.id}"


def _parse_cursor(raw: str | None) -> tuple[dt.datetime, str] | None:
    """Decode the opaque cursor; ``None`` (or anything malformed) → first page."""
    if not raw:
        return None
    at_str, sep, event_id = raw.partition("|")
    if not sep:
        return None
    try:
        at = dt.datetime.fromisoformat(at_str)
    except ValueError:
        return None
    return (at, event_id)


@router.get("/", response=TimelineOut, summary="Team activity timeline")
def list_timeline(
    request: HttpRequest,
    subsystem: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    before: str | None = None,
) -> TimelineOut:
    """Recent activity across all subsystems, newest first.

    - ``subsystem``: restrict to one filter key (unknown keys → all).
    - ``limit``: page size, clamped to 1..200.
    - ``before``: opaque cursor from a prior page's ``next_before`` — return only
      events older than it.
    """
    limit = clamp_limit(limit, cap=_MAX_LIMIT)
    sub = sources.valid_subsystem(subsystem)
    cursor = _parse_cursor(before)
    # Scope to the caller's workspaces, mirroring the agents surface: a
    # `/api/w/{ws}/` prefix pins one workspace (membership-gated upstream), a flat
    # `/api/` call spans every workspace the user belongs to. Sources opt into the
    # scope (see sources._call_source); framework never imports product models.
    ws = getattr(request, "workspace_slug", None)
    workspace_slugs = {ws} if ws else wsvc.user_workspace_slugs(request.user)
    events = sources.gather(
        subsystem=sub,
        limit=limit,
        before=cursor,
        user=request.user,
        workspace_slugs=workspace_slugs,
    )
    # A full page means there may be more; the tail event seeds the next cursor.
    next_before = _encode_cursor(events[-1]) if len(events) == limit else None
    return TimelineOut(
        events=[ActivityEventOut(**dataclasses.asdict(e)) for e in events],
        subsystems=[SubsystemOut(**s) for s in sources.subsystem_catalog()],
        next_before=next_before,
    )
