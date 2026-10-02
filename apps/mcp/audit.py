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
    workspace: str | None = None,
) -> None:
    from apps.mcp.models import MCPAuditLog

    MCPAuditLog.objects.create(
        user_id=user_id,
        workspace_slug=_attributed_workspace(user_id, workspace),
        tool=tool,
        args_summary=args_summary[:500],
        ok=ok,
        error=(error or "")[:500],
    )


async def write_audit(
    *,
    user_id: int | None,
    tool: str,
    args_summary: str = "",
    ok: bool = True,
    error: str = "",
    workspace: str | None = None,
) -> None:
    try:
        await sync_to_async(_write_audit_sync, thread_sensitive=True)(
            user_id=user_id,
            tool=tool,
            args_summary=args_summary,
            ok=ok,
            error=error,
            workspace=workspace,
        )
    except Exception:  # noqa: BLE001 — audit must never break a tool
        logger.exception("Failed to write MCP audit log for tool=%s", tool)
