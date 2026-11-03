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
from apps.api.route_gates import gate
from apps.events import after_record, services
from apps.events.models import Event
from apps.events.schemas import EventBatchIn, EventListOut, EventRecordOut, SettingChangeListOut
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

router = Router(auth=session_auth, tags=["events"])
# The settings audit trail's reader is its own router so `apps/mcp/audit_api.py` (the MCP call
# log, `/events/mcp-calls`) can be mounted between the two and keep the published path order.
settings_router = Router(auth=session_auth, tags=["events"])


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
@gate("events.write", kind="telemetry")  # in the pinned or default workspace
def record_events(request: HttpRequest, payload: EventBatchIn) -> dict:
    """Repeats of ``(workspace, source, key)`` coalesce onto one row with a
    count, so a permanently-stuck retry loop stays one row instead of one row
    per tick. A blank key never coalesces."""
    pinned = getattr(request, "workspace_slug", None)
    # A box posting with its own runner token records in the runner's workspace
    # when nothing is pinned: its owner may belong to several, and a credential
    # alert that 422'd for want of a default was an alert nobody got.
    # Only a workspace the owner may record in: `Runner.workspace` is where the
    # box was paired, which the owner may since have lost write in — then the
    # owner's default, as for any caller.
    rtok = getattr(request, "runner_token", None)
    runner_home = None
    if rtok is not None and rtok.runner.workspace_id:
        ws = rtok.runner.workspace
        if perms.can(request.user, ws, perms.EVENTS_WRITE):
            runner_home = ws
    home = (
        wsvc.Workspace.objects.filter(slug=pinned).first() if pinned else None
    ) or runner_home or wsvc.user_default_workspace(request.user)
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
    items = [item.model_dump() for item in payload.items]
    first_today: list[int] = []
    result = services.record(items, workspace=home, first_today=first_today)
    if rtok is not None:
        # Recording alone tells nobody: a box's credential alert also reaches
        # the people who can fix it, once a day per alert — the harness registered for it.
        for index in first_today:
            after_record.first_today(rtok.runner, items[index])
    return result


@router.get("/", response=EventListOut, summary="List events")
@gate("logs.read")  # non-admin gets no rows, not 403
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
        workspace_slugs=perms.request_read_slugs_with(request, perms.LOGS_READ),
        source=source,
        kind=kind,
        level=level,
        since=since,
        limit=limit,
    )
    return {"items": [_out(ev) for ev in qs]}


# The settings audit trail (`apps/events/audit.py`): one row per change to a
# setting, whichever entry point made it — web UI, CLI, script or MCP tool.
# Read on the same tier as the MCP call log: a workspace's admins see its
# changes; anyone sees the changes they made themselves.
@settings_router.get("/setting-changes", response=SettingChangeListOut,
            summary="Changes to settings in your workspaces (admins), and your own")
@gate("logs.read", "self")  # the workspace's changes for admins; your own always
def list_setting_changes(
    request: HttpRequest,
    operation: str | None = None,
    target: str | None = None,
    actor: str | None = None,
    since_minutes: int | None = None,
    limit: int = 100,
) -> dict:
    """Newest first: who changed which setting, through what (`via`: ui, mcp,
    cli, api, runner), the request with secrets withheld, and the state before
    and after where the route reports it. ``operation`` is a prefix match on the
    route name (``set_agent`` covers every agent setting); ``target`` is a
    substring match (an agent slug, a person's address); ``actor`` an exact
    email."""
    from apps.events import audit

    qs = audit.visible_to(request).select_related("workspace")
    if operation:
        qs = qs.filter(operation__startswith=operation)
    if target:
        qs = qs.filter(target__icontains=target)
    if actor:
        qs = qs.filter(actor_email__iexact=actor)
    if since_minutes:
        qs = qs.filter(created_at__gte=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=max(1, since_minutes)))
    rows = qs.order_by("-created_at")[: max(1, min(limit, 500))]
    return {"items": [{
        "id": r.pk,
        "created_at": r.created_at.isoformat(),
        "workspace": r.workspace.slug if r.workspace else "",
        "actor_email": r.actor_email,
        "via": r.via,
        "credential": r.credential or {},
        "client": r.client,
        "mcp_tool": r.mcp_tool,
        "request_id": r.request_id,
        "operation": r.operation,
        "method": r.method,
        "path": r.path,
        "target": r.target,
        "summary": r.summary,
        "request": r.request or {},
        "changes": r.changes or [],
    } for r in rows]}
