"""Schedule MCP tools — run as the authenticated user.

Six tools (list/create/update/delete/run-now/preview) over AgentSchedule. Each
calls apps.harness.schedule_services — the SAME layer the REST routes call, so
the two surfaces can't drift. Each tool: resolves the user, (writes) enforces
the per-user rate limit, runs the ORM work via sync_to_async, and audits both
the success and error paths.
"""
from __future__ import annotations

import datetime as dt

from asgiref.sync import sync_to_async
from canopy_cron import validate_cron, validate_timezone
from django.contrib.auth import get_user_model

from apps.harness import schedule_services as ss
from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.rate_limit import RateLimitError, check_write_limit
from apps.mcp.server import mcp

User = get_user_model()


def _user(user_id: int):
    return User.objects.get(pk=user_id)


@mcp.tool
async def list_schedules(agent_slug: str, limit: int = 100) -> list[dict]:
    """List an agent's recurring schedules (cron config + next fire times)."""
    user_id = current_user_id()
    try:
        rows = await sync_to_async(_list_sync, thread_sensitive=True)(user_id, agent_slug, limit)
    except Exception as exc:  # noqa: BLE001
        await write_audit(user_id=user_id, tool="list_schedules",
                          args_summary=f"agent={agent_slug}", ok=False, error=str(exc))
        raise
    await write_audit(user_id=user_id, tool="list_schedules",
                      args_summary=f"agent={agent_slug} -> {len(rows)} rows", ok=True)
    return rows


def _list_sync(user_id, agent_slug, limit):
    user = _user(user_id)
    scheds = ss.list_schedules(user, agent_slug)[:limit]
    return [ss.serialize_schedule(s) for s in scheds]


@mcp.tool
async def preview_cron(agent_slug: str, cron: str, timezone: str = "UTC") -> list[str]:
    """Preview the next 3 fire times for a cron+timezone, as ISO-8601 strings.
    Computed with the same slot math the runner fires on — never re-implement cron."""
    user_id = current_user_id()
    try:
        runs = await sync_to_async(_preview_sync, thread_sensitive=True)(user_id, agent_slug, cron, timezone)
    except Exception as exc:  # noqa: BLE001
        await write_audit(user_id=user_id, tool="preview_cron",
                          args_summary=f"agent={agent_slug} cron={cron!r}", ok=False, error=str(exc))
        raise
    await write_audit(user_id=user_id, tool="preview_cron",
                      args_summary=f"agent={agent_slug} cron={cron!r}", ok=True)
    return runs


def _preview_sync(user_id, agent_slug, cron, timezone):
    validate_cron(cron)
    validate_timezone(timezone)
    user = _user(user_id)
    return [d.isoformat() for d in ss.preview_cron(user, agent_slug, cron, timezone)]


def _parse_when(raw: str | None) -> dt.datetime | None:
    """ISO-8601 -> datetime. A value without an offset stays naive; the service
    reads it in the schedule's timezone, so every surface agrees on what
    '2026-09-28T09:00' means."""
    if not raw:
        return None
    return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))


@mcp.tool
async def create_schedule(
    agent_slug: str, name: str, prompt: str, cron: str = "", timezone: str = "UTC",
    enabled: bool = True, routing: str = "prefer_local", grace_minutes: int = 120,
    notify: list[str] | None = None, run_once_at: str | None = None,
) -> dict:
    """Create a scheduled turn for an agent. `prompt` is what the turn is seeded
    with; `timezone` is an IANA name.

    Give exactly ONE of:
    - `cron` — a 5-field expression, for a RECURRING turn;
    - `run_once_at` — an ISO-8601 instant, for a ONE-OFF turn (a reminder, a
      follow-up). It fires once and then disables itself. Without an offset it is
      read in `timezone`: run_once_at="2026-09-28T09:00", timezone="America/Denver"
      is 9am Denver time."""
    user_id = current_user_id()
    summary = f"agent={agent_slug} name={name!r}"
    if user_id is not None:
        try:
            check_write_limit(user_id)
        except RateLimitError as exc:
            await write_audit(user_id=user_id, tool="create_schedule",
                              args_summary=summary, ok=False, error=str(exc))
            raise
    try:
        row = await sync_to_async(_create_sync, thread_sensitive=True)(
            user_id, agent_slug, name, prompt, cron, timezone, enabled, routing,
            grace_minutes, notify or ["inbox"], run_once_at,
        )
    except Exception as exc:  # noqa: BLE001
        await write_audit(user_id=user_id, tool="create_schedule",
                          args_summary=summary, ok=False, error=str(exc))
        raise
    await write_audit(user_id=user_id, tool="create_schedule",
                      args_summary=summary, ok=True)
    return row


def _create_sync(user_id, agent_slug, name, prompt, cron, timezone, enabled, routing, grace_minutes,
                 notify, run_once_at=None):
    if cron:
        validate_cron(cron)
    validate_timezone(timezone)
    user = _user(user_id)
    fields = dict(name=name, prompt=prompt, cron=cron or None, timezone=timezone, enabled=enabled,
                  routing=routing, grace_minutes=grace_minutes, notify=notify,
                  run_once_at=_parse_when(run_once_at))
    return ss.serialize_schedule(ss.create_schedule(user, agent_slug, fields))


@mcp.tool
async def update_schedule(
    agent_slug: str, schedule_id: int, name: str | None = None, prompt: str | None = None,
    cron: str | None = None, timezone: str | None = None, enabled: bool | None = None,
    routing: str | None = None, grace_minutes: int | None = None, notify: list[str] | None = None,
    run_once_at: str | None = None,
) -> dict:
    """Update a schedule. Only the fields you pass are changed. Passing `cron`
    makes it recurring; passing `run_once_at` (ISO-8601, read in the schedule's
    timezone when it has no offset) makes it a one-off, or re-arms one that fired."""
    user_id = current_user_id()
    summary = f"agent={agent_slug} id={schedule_id}"
    if user_id is not None:
        try:
            check_write_limit(user_id)
        except RateLimitError as exc:
            await write_audit(user_id=user_id, tool="update_schedule",
                              args_summary=summary, ok=False, error=str(exc))
            raise
    raw = dict(name=name, prompt=prompt, cron=cron, timezone=timezone, enabled=enabled,
               routing=routing, grace_minutes=grace_minutes, notify=notify,
               run_once_at=run_once_at)
    fields = {k: v for k, v in raw.items() if v is not None}
    try:
        row = await sync_to_async(_update_sync, thread_sensitive=True)(user_id, agent_slug, schedule_id, fields)
    except Exception as exc:  # noqa: BLE001
        await write_audit(user_id=user_id, tool="update_schedule",
                          args_summary=summary, ok=False, error=str(exc))
        raise
    await write_audit(user_id=user_id, tool="update_schedule",
                      args_summary=f"{summary} fields={sorted(fields)}", ok=True)
    return row


def _update_sync(user_id, agent_slug, schedule_id, fields):
    if "cron" in fields:
        validate_cron(fields["cron"])
    if "timezone" in fields:
        validate_timezone(fields["timezone"])
    user = _user(user_id)
    if "run_once_at" in fields:
        fields["run_once_at"] = _parse_when(fields["run_once_at"])
    return ss.serialize_schedule(ss.update_schedule(user, agent_slug, schedule_id, fields))


@mcp.tool
async def delete_schedule(agent_slug: str, schedule_id: int) -> dict:
    """Delete a schedule. Any open occurrences it fired are retired first."""
    user_id = current_user_id()
    summary = f"agent={agent_slug} id={schedule_id}"
    if user_id is not None:
        try:
            check_write_limit(user_id)
        except RateLimitError as exc:
            await write_audit(user_id=user_id, tool="delete_schedule",
                              args_summary=summary, ok=False, error=str(exc))
            raise
    try:
        await sync_to_async(_delete_sync, thread_sensitive=True)(user_id, agent_slug, schedule_id)
    except Exception as exc:  # noqa: BLE001
        await write_audit(user_id=user_id, tool="delete_schedule",
                          args_summary=summary, ok=False, error=str(exc))
        raise
    await write_audit(user_id=user_id, tool="delete_schedule",
                      args_summary=summary, ok=True)
    return {"deleted": schedule_id}


def _delete_sync(user_id, agent_slug, schedule_id):
    ss.delete_schedule(_user(user_id), agent_slug, schedule_id)


@mcp.tool
async def run_schedule_now(agent_slug: str, schedule_id: int) -> dict:
    """Trigger a schedule off-cycle NOW. Spawns a real agent turn (tokens)."""
    user_id = current_user_id()
    summary = f"agent={agent_slug} id={schedule_id}"
    if user_id is not None:
        # A rate-limited call here is the abuse signature we most need a trail
        # for: this is the one tool that burns tokens, so an AI looping on it
        # is exactly what trips the limit.
        try:
            check_write_limit(user_id)
        except RateLimitError as exc:
            await write_audit(user_id=user_id, tool="run_schedule_now",
                              args_summary=summary, ok=False, error=str(exc))
            raise
    try:
        row, name = await sync_to_async(_run_now_sync, thread_sensitive=True)(user_id, agent_slug, schedule_id)
    except Exception as exc:  # noqa: BLE001
        await write_audit(user_id=user_id, tool="run_schedule_now",
                          args_summary=summary, ok=False, error=str(exc))
        raise
    # The name is in the summary on purpose: run_now is the one tool that burns
    # tokens, so a runaway must be visible in MCPAuditLog rather than inferred.
    await write_audit(user_id=user_id, tool="run_schedule_now",
                      args_summary=f"agent={agent_slug} id={schedule_id} name={name!r}", ok=True)
    return row


def _run_now_sync(user_id, agent_slug, schedule_id):
    sched = ss.run_schedule_now(_user(user_id), agent_slug, schedule_id)
    return ss.serialize_schedule(sched), sched.name
