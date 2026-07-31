"""Stored-session maintenance MCP tools — run as the authenticated user.

`audit_session_noise` (read) and `purge_session_noise` (write) call the SAME
shared functions as the `purge_transcript_noise` management command
(`apps.canopy_sessions.maintenance`), so the two surfaces can never drift.

This exists because the alternative was reaching prod data with a one-off
`aws ecs run-task` command override: unrepeatable, unauditable, and gated on
whoever held AWS credentials. Over MCP the same work is a normal authenticated
call — scoped to the caller's workspaces, rate-limited, and written to the audit
log like every other write.

Every tool:
  * resolves the authenticated user from the access token,
  * scopes to that user's workspaces (empty scope sees NOTHING — fails closed),
  * (for writes) enforces a per-user rate limit,
  * runs the ORM work via sync_to_async,
  * writes an MCPAuditLog row (best-effort).
"""
from __future__ import annotations

from asgiref.sync import sync_to_async

from apps.canopy_sessions import maintenance
from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.rate_limit import RateLimitError, check_write_limit
from apps.mcp.server import mcp
from apps.workspaces import services as wsvc


@mcp.tool
async def audit_session_noise(
    session_id: str | None = None,
    sample: int = 5,
) -> dict:
    """Report harness output stored as USER messages in your sessions.

    Claude Code writes task notifications, system reminders, skill bodies and
    interrupt markers as `type: "user"` transcript records, so they can land in
    the store looking like something a person typed. Ingest filters them now;
    this finds rows written before a given prefix was recognised.

    Args:
      - session_id: restrict to one session (UUID). Omit for all your sessions.
      - sample: example rows to include (clamped to 50; default 5).

    Returns {"matched", "sessions", "sample": [{session_id, workspace,
    turn_index, text}]}. Read-only — see `purge_session_noise` to delete.
    """
    user_id = current_user_id()
    summary = f"session_id={session_id} sample={sample}"
    slugs = await sync_to_async(wsvc.workspace_slugs_for_user_id, thread_sensitive=True)(user_id)
    try:
        report = await sync_to_async(maintenance.audit_noise, thread_sensitive=True)(
            workspace_slugs=slugs, session_id=session_id, sample=sample,
        )
    except Exception as exc:  # noqa: BLE001
        await write_audit(
            user_id=user_id, tool="audit_session_noise",
            args_summary=summary, ok=False, error=str(exc),
        )
        raise
    await write_audit(
        user_id=user_id, tool="audit_session_noise",
        args_summary=f"{summary} -> matched={report['matched']}", ok=True,
    )
    return report


@mcp.tool
async def purge_session_noise(
    session_id: str | None = None,
    apply: bool = False,
    sample: int = 5,
) -> dict:
    """Delete harness output stored as USER messages in your sessions.

    DRY RUN BY DEFAULT: without `apply=True` this reports what WOULD go and
    deletes nothing. Deleting is irreversible and the match is only as good as
    the current prefix list, so the intended sequence is audit -> read the count
    -> apply.

    Args:
      - session_id: restrict to one session (UUID). Omit for all your sessions.
      - apply: actually delete. Default False.
      - sample: example rows to include (clamped to 50; default 5).

    Returns the audit report plus {"applied", "deleted"}. Scoped to your
    workspaces; rate-limited per user.
    """
    user_id = current_user_id()
    summary = f"session_id={session_id} apply={apply} sample={sample}"

    # Rate-limit only the destructive call. A dry run is a read wearing this
    # tool's name, and making it cost a write slot would push callers toward
    # skipping straight to apply — the opposite of what the gate is for.
    if apply and user_id is not None:
        try:
            check_write_limit(user_id)
        except RateLimitError as exc:
            await write_audit(
                user_id=user_id, tool="purge_session_noise",
                args_summary=summary, ok=False, error=str(exc),
            )
            raise

    slugs = await sync_to_async(wsvc.workspace_slugs_for_user_id, thread_sensitive=True)(user_id)
    try:
        report = await sync_to_async(maintenance.purge_noise, thread_sensitive=True)(
            workspace_slugs=slugs, session_id=session_id, sample=sample, apply=apply,
        )
    except Exception as exc:  # noqa: BLE001
        await write_audit(
            user_id=user_id, tool="purge_session_noise",
            args_summary=summary, ok=False, error=str(exc),
        )
        raise

    await write_audit(
        user_id=user_id, tool="purge_session_noise",
        args_summary=f"{summary} -> matched={report['matched']} deleted={report['deleted']}",
        ok=True,
    )
    return report
