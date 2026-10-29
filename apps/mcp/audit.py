"""Audit + user-resolution helpers shared by all MCP tools.

`current_user_id()` reads the authenticated user out of the FastMCP
access token (set by CanopyPATVerifier or the OAuth provider).

`write_audit()` records one MCPAuditLog row. It is best-effort: an audit
failure is swallowed so it can never mask a tool's real result.

Both DB-touching helpers are exposed as async wrappers because tools run
in the MCP event loop.
"""
from __future__ import annotations

import logging

from asgiref.sync import sync_to_async
from fastmcp.server.dependencies import get_access_token

logger = logging.getLogger(__name__)


def current_user_id() -> int | None:
    """Return the authenticated Django user id, or None if unauthenticated."""
    token = get_access_token()
    if token is None:
        return None
    claims = token.claims or {}
    uid = claims.get("user_id")
    if uid is not None:
        try:
            return int(uid)
        except (TypeError, ValueError):
            pass
    sub = claims.get("sub")
    if sub is not None:
        try:
            return int(sub)
        except (TypeError, ValueError):
            return None
    return None


def _attributed_workspace(user_id: int | None, workspace: str | None) -> str:
    """The workspace a call is filed under: the one it named, else the caller's
    sole workspace, else none."""
    if workspace:
        return workspace[:64]
    if user_id is None:
        return ""
    from django.contrib.auth import get_user_model

    from apps.workspaces import services as wsvc

    user = get_user_model().objects.filter(pk=user_id).first()
    ws = wsvc.user_default_workspace(user) if user is not None else None
    return ws.slug if ws is not None else ""


def _write_audit_sync(
    *, user_id: int | None, tool: str, args_summary: str, ok: bool, error: str,
    workspace: str | None = None, acting_agent: str = "", act_as_grant: str = "",
    via_mcp: bool = True,
) -> None:
    from django.contrib.auth import get_user_model

    from apps.events import activity
    from apps.mcp.models import MCPAuditLog

    row = MCPAuditLog.objects.create(
        user_id=user_id,
        workspace_slug=_attributed_workspace(user_id, workspace),
        tool=tool,
        args_summary=args_summary[:500],
        ok=ok,
        error=(error or "")[:500],
        acting_agent=(acting_agent or "")[:64],
        act_as_grant=(act_as_grant or "")[:36],
    )
    if row.workspace_slug and via_mcp:
        # A call attributed to no workspace (a flat call by someone in several)
        # has no tenant to count it in, so it is the audit log's alone.
        actor = get_user_model().objects.filter(pk=user_id).first() if user_id else None
        activity.emit(activity.MCP_CALL, workspace=row.workspace_slug, actor=actor, surface="mcp",
                      ref=f"mcp{row.pk}", meta={"tool": tool, "ok": ok}, at=row.created_at)


async def write_audit(
    *,
    user_id: int | None,
    tool: str,
    args_summary: str = "",
    ok: bool = True,
    error: str = "",
    workspace: str | None = None,
    acting_agent: str = "",
    act_as_grant: str = "",
) -> None:
    try:
        await sync_to_async(_write_audit_sync, thread_sensitive=True)(
            user_id=user_id,
            tool=tool,
            args_summary=args_summary,
            ok=ok,
            error=error,
            workspace=workspace,
            acting_agent=acting_agent,
            act_as_grant=act_as_grant,
        )
    except Exception:  # noqa: BLE001 — audit must never break a tool
        logger.exception("Failed to write MCP audit log for tool=%s", tool)


def log_act_as_rest_call(request, grant, status: int) -> None:
    """The call-log row for a REST request an act-as-you token made — the same row an MCP
    call writes, so every request under the permission reads "<agent> as <person>"
    (`tokens.middleware.act_as_call_log`)."""
    from .api_tools import tool_operation  # noqa: PLC0415 — api_tools loads fastmcp and the API schema; only an act-as REST request pays

    method = request.method.upper()
    path = request.path_info
    _write_audit_sync(
        user_id=grant.person_id,
        tool=tool_operation(method, path) or f"{method} {path}"[:200],
        args_summary=f"REST {method} {path}",
        ok=bool(status) and status < 400,
        error="" if status < 400 else f"HTTP {status}",
        workspace=getattr(request, "workspace_slug", None),
        acting_agent=grant.agent.slug,
        act_as_grant=str(grant.pk),
        via_mcp=False,  # the analytics fact is an MCP call's; a REST request is not one
    )
