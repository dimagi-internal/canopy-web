"""The MCP audit log's reader: `GET /api/events/mcp-calls` (mounted under `/events`, where it was published)."""
from __future__ import annotations

import datetime as dt

from django.db.models import Q
from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.workspaces import permissions as perms

from .models import MCPAuditLog
from .schemas import McpCallListOut

router = Router(auth=session_auth, tags=["events"])


# A different log from the event log, read on the same tier: the MCP audit log
# (`apps/mcp/models.MCPAuditLog`), one row per tool call made through canopy's
# MCP server — by a person's assistant, an agent session, or a script. Before
# this it had no reader at all; only aggregates reached owners through a
# connected site's traffic health.
@router.get("/mcp-calls", response=McpCallListOut,
            summary="MCP tool calls made in your workspaces (admins), and your own")
@gate("logs.read", "self")  # the workspace's calls for admins; your own always
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
    # The workspace's calls on the logs tier; your own calls always, wherever
    # they were filed (a call attributed to no workspace is its caller's alone).
    scope = Q(workspace_slug__in=perms.request_read_slugs_with(request, perms.LOGS_READ))
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
        "acting_agent": r.acting_agent,
        "act_as_grant": r.act_as_grant,
    } for r in rows]}
