"""Share the session you are working in to a Slack channel.

The Slack front door is inbound — a thread is born in Slack and adopts a
session. This is the outbound half: the session (usually a Claude Code session
running the canopy plugin's `/canopy:share-to-slack`) writes a summary of what
it is doing and posts it. All the rules live in `apps/slack/share.py`; this is
the MCP wrapper, rate-limited and audited like every write tool.
"""
from __future__ import annotations

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.rate_limit import RateLimitError, check_write_limit
from apps.mcp.server import mcp


def _share_sync(user_id, channel, summary, mode, session_id, claude_session_id,
                emdash_task, emdash_project, workspace) -> dict:
    from apps.slack import share

    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        raise PermissionError("Not authenticated.")
    try:
        session = share.resolve_session(
            user, session_id=session_id, claude_session_id=claude_session_id,
            emdash_task=emdash_task, emdash_project=emdash_project,
        )
    except share.Ambiguous:
        raise ValueError(
            "More than one canopy session matches that emdash task; pass claude_session_id "
            "(or session_id) to say which.") from None
    result = share.share_session(user, channel=channel, summary=summary, mode=mode,
                                 session=session, workspace=workspace)
    if not result.ok:
        raise ValueError(result.message)
    return {
        "status": result.status,
        "message": result.message,
        "mode": mode,
        "channel": result.channel_id,
        "ts": result.ts,
        "permalink": result.permalink,
        "session_id": str(result.session.pk) if result.session else None,
    }


@mcp.tool
async def share_session_to_slack(
    channel: str,
    summary: str,
    mode: str = "broadcast",
    claude_session_id: str = "",
    emdash_task: str = "",
    emdash_project: str = "",
    session_id: str = "",
    workspace: str = "",
) -> dict:
    """Post a summary of the current session into a Slack channel.

    `channel` is a channel name (`#dev`) or id; the canopy app must be in it.
    `summary` is Markdown — what we are doing, why, and where it stands.

    `mode`:
      * `broadcast` (default) — one post; replies in Slack stay between people.
      * `bind` — the post starts a thread that is bound to this session from
        now on, exactly as if the session had been started from Slack: the
        session's replies appear in the thread and teammates' replies in the
        thread reach the session.

    Identify the session with `claude_session_id` ($CLAUDE_CODE_SESSION_ID),
    else `emdash_task` + `emdash_project` ($EMDASH_TASK_NAME and the basename of
    $EMDASH_ROOT_PATH), else `session_id` (canopy's own id). A broadcast works
    without one; a bind does not. `workspace` is needed only when you belong to
    several workspaces with Slack connected and no session is given.
    """
    user_id = current_user_id()
    summary_line = f"channel={channel} mode={mode} claude={claude_session_id[:12]} task={emdash_task}"
    if user_id is not None:
        try:
            check_write_limit(user_id)
        except RateLimitError as exc:
            await write_audit(user_id=user_id, tool="share_session_to_slack",
                              args_summary=summary_line, ok=False, error=str(exc))
            raise
    try:
        out = await sync_to_async(_share_sync, thread_sensitive=True)(
            user_id, channel, summary, mode, session_id, claude_session_id,
            emdash_task, emdash_project, workspace,
        )
    except Exception as exc:  # noqa: BLE001
        await write_audit(user_id=user_id, tool="share_session_to_slack",
                          args_summary=summary_line, ok=False, error=str(exc))
        raise
    await write_audit(user_id=user_id, tool="share_session_to_slack",
                      args_summary=summary_line, ok=True)
    return out
