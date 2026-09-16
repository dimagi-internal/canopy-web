"""Insight MCP tools — run as the authenticated user.

`list_insights` (read) and `clear_insights` (write) call the SAME shared
service functions as the REST views (`apps.projects.services`), so the
two surfaces can never drift. Each tool:

  * resolves the authenticated user from the access token,
  * (for writes) enforces a per-user rate limit,
  * runs the ORM work via sync_to_async,
  * writes an MCPAuditLog row (best-effort).
"""
from __future__ import annotations

from asgiref.sync import sync_to_async

from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.rate_limit import RateLimitError, check_write_limit
from apps.mcp.server import mcp
from apps.projects import services
from apps.workspaces import services as wsvc


@mcp.tool
async def list_insights(
    category: str | None = None,
    source: str | None = None,
    project: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """List insight cards from the canopy-web feed.

    Filters (all optional, AND-combined):
      - category: insight content tagged "[<category>]"
      - source: the source that posted the insight
      - project: project slug
      - limit: max rows (clamped to 100; default 20)
    """
    user_id = current_user_id()
    slugs = await sync_to_async(wsvc.workspace_slugs_for_user_id, thread_sensitive=True)(user_id)
    try:
        rows = await sync_to_async(services.list_insights, thread_sensitive=True)(
            workspace_slugs=slugs, category=category, source=source, project=project, limit=limit
        )
    except Exception as exc:  # noqa: BLE001
        await write_audit(
            user_id=user_id, tool="list_insights",
            args_summary=f"category={category} source={source} project={project} limit={limit}",
            ok=False, error=str(exc),
        )
        raise
    await write_audit(
        user_id=user_id, tool="list_insights",
        args_summary=f"category={category} source={source} project={project} limit={limit} -> {len(rows)} rows",
        ok=True,
    )
    return rows


@mcp.tool
async def clear_insights(
    source: str | None = None,
    category: str | None = None,
    project: str | None = None,
    older_than_days: int | None = None,
) -> dict:
    """Delete insights matching the given filters; returns {"cleared": N}.

    Same filters as the REST clear endpoint (all optional, AND-combined):
      - source, category, project (slug), older_than_days.
    A call with NO filters clears ALL insights — intended, so be careful.

    Rate-limited per user.
    """
    user_id = current_user_id()
    summary = (
        f"source={source} category={category} project={project} older_than_days={older_than_days}"
    )

    if user_id is not None:
        try:
            check_write_limit(user_id)
        except RateLimitError as exc:
            await write_audit(
                user_id=user_id, tool="clear_insights",
                args_summary=summary, ok=False, error=str(exc),
            )
            raise

    slugs = await sync_to_async(wsvc.workspace_slugs_for_user_id, thread_sensitive=True)(user_id)
    try:
        cleared = await sync_to_async(services.clear_insights, thread_sensitive=True)(
            workspace_slugs=slugs, source=source, category=category,
            project=project, older_than_days=older_than_days,
        )
    except Exception as exc:  # noqa: BLE001
        await write_audit(
            user_id=user_id, tool="clear_insights",
            args_summary=summary, ok=False, error=str(exc),
        )
        raise

    await write_audit(
        user_id=user_id, tool="clear_insights",
        args_summary=f"{summary} -> cleared={cleared}", ok=True,
    )
    return {"cleared": cleared}


@mcp.tool
async def dismiss_insights(ids: list[int]) -> dict:
    """Dismiss specific insights by id; returns {"dismissed": [ids that went]}.

    Use this for "close the ones I am looking at". `current_page` gives you the
    ids on the user's screen; pass exactly those.

    Prefer this over `clear_insights` whenever the request is about a visible
    set. `clear_insights` takes FILTERS, and a filter is only an approximation
    of what somebody can see — on a paginated feed it also matches rows below
    the fold they never looked at, and with no filters at all it deletes
    everything.

    Ids outside the caller's workspaces are absent from the result rather than
    an error, so compare the returned list against what you asked for.

    Rate-limited per user.
    """
    user_id = current_user_id()
    summary = f"ids={ids[:20]}{'...' if len(ids) > 20 else ''}"

    if user_id is not None:
        try:
            check_write_limit(user_id)
        except RateLimitError as exc:
            await write_audit(
                user_id=user_id, tool="dismiss_insights",
                args_summary=summary, ok=False, error=str(exc),
            )
            raise

    slugs = await sync_to_async(wsvc.workspace_slugs_for_user_id, thread_sensitive=True)(user_id)
    try:
        dismissed = await sync_to_async(services.dismiss_insights, thread_sensitive=True)(
            workspace_slugs=slugs, ids=list(ids or []),
        )
    except Exception as exc:  # noqa: BLE001
        await write_audit(
            user_id=user_id, tool="dismiss_insights",
            args_summary=summary, ok=False, error=str(exc),
        )
        raise

    await write_audit(user_id=user_id, tool="dismiss_insights", args_summary=summary)
    return {"dismissed": dismissed}
