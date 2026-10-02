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
from apps.events.schemas import EventBatchIn, EventListOut, EventRecordOut, McpCallListOut
from apps.workspaces import permissions as perms
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
    # The fleet log is read by owners deciding what is broken, so writing to it
    # is the author tier. A viewer could otherwise forge "runner.credential"
    # alarms, or bump a real event's count and summary through coalescing. The
    # producers that matter are runners, which post with their OWNER's token,
    # and pairing an agent's box already needs more than viewer.
    if not perms.can(request.user, home, perms.EVENTS_WRITE):
        raise HttpError(403, "recording events requires the editor role in this workspace")
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
    # The event log is a LOG: the workspace admin's (`permissions.LOGS_READ`).
    # It names who read which Slack channel, which mailbox failed, which turn
    # errored — every member, viewers included, used to read all of it. Scoped
    # like every request: a pinned `/api/w/{ws}/` read is about that one
    # workspace; a non-admin simply sees no rows rather than a 403, so the same
    # call works for someone who is admin in one workspace and not another.
    qs = services.list_events(
        workspace_slugs=perms.request_slugs_with(request, perms.LOGS_READ),
        source=source,
        kind=kind,
        level=level,
        since=since,
        limit=limit,
    )
    return {"items": [_out(ev) for ev in qs]}


# A different log from the event log, read on the same tier: the MCP audit log
# (`apps/mcp/models.MCPAuditLog`), one row per tool call made through canopy's
# MCP server — by a person's assistant, an agent session, or a script. Before
# this it had no reader at all; only aggregates reached owners through a
# connected site's traffic health.
@router.get("/mcp-calls", response=McpCallListOut,
            summary="MCP tool calls made in your workspaces (admins), and your own")
def list_mcp_calls(
    request: HttpRequest,
    tool: str | None = None,
    failed: bool = False,
    since_minutes: int | None = None,
    limit: int = 100,
) -> dict:
    """Newest first. A workspace admin or owner sees every call made in that
    workspace; anyone sees their own calls. ``tool`` is a prefix match;
    ``failed`` keeps only calls that errored."""
    from django.db.models import Q

    from apps.mcp.models import MCPAuditLog

    # The workspace's calls on the logs tier; your own calls always, wherever
    # they were filed (a call attributed to no workspace is its caller's alone).
    scope = Q(workspace_slug__in=perms.request_slugs_with(request, perms.LOGS_READ))
    pinned = getattr(request, "workspace_slug", None)
    mine = Q(user=request.user) & (Q(workspace_slug=pinned) if pinned else Q())
    qs = MCPAuditLog.objects.filter(scope | mine).select_related("user")
    if tool:
        qs = qs.filter(tool__startswith=tool)
    if failed:
        qs = qs.filter(ok=False)
    if since_minutes:
        qs = qs.filter(created_at__gte=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=max(1, since_minutes)))
    rows = qs.order_by("-created_at")[: max(1, min(limit, 500))]
    return {"items": [{
        "id": r.pk,
        "created_at": r.created_at.isoformat(),
        "workspace": r.workspace_slug,
        "user_email": (r.user.email if r.user else ""),
        "tool": r.tool,
        "args_summary": r.args_summary,
        "ok": r.ok,
        "error": r.error,
    } for r in rows]}
