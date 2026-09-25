"""Request-free schedule service layer.

The MCP invariant (apps/mcp/tools/insights.py) is that tools call the SAME
service functions as the REST views, so the two surfaces can't drift. Schedules
had no such layer — create/update/delete were inline in the Ninja handlers — so
this module extracts them. It takes a `user` (not a `request`) and raises domain
exceptions (not HttpError), so both the REST routes and the MCP tools can call
it. REST maps the exceptions to HTTP; the MCP re-raises them after auditing.

Reuses apps.harness.services for the turn-lifecycle operations
(supersede_open_turns / run_schedule_now / latest_occurrence_turn).
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from canopy_cron import next_slots, slots_between, validate_timezone
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.agents.models import Agent
from apps.workspaces import services as wsvc

from . import services
from .models import AgentSchedule


class ScheduleNotFound(Exception):
    """Agent missing / wrong tenant / non-member / schedule not under this agent.

    One type for all four, so a non-member cannot distinguish 'no such agent'
    from 'not yours' — existence never leaks."""


class ScheduleForbidden(Exception):
    """The caller is a MEMBER of the agent's workspace but holds too low a role.

    Deliberately separate from ScheduleNotFound, and the distinction is the
    whole point: 404 means "you cannot see this", 403 means "you can see it and
    may not do this". Collapsing them would make every refusal unexplainable to
    the person most likely to hit one — a viewer on their own team.
    """

    def __init__(self, agent_slug: str, *, required: str) -> None:
        super().__init__(agent_slug)
        self.agent_slug = agent_slug
        self.required = required


class DuplicateScheduleName(Exception):
    """The uniq_agent_schedule_name constraint was violated on create/update."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class InvalidSchedule(ValueError):
    """The timing fields do not describe a schedule that can fire: neither or both
    of cron / run_once_at, or a one-off whose instant has already passed. REST
    maps it to 422; the MCP re-raises it with the message intact."""


def one_off_cron(when: dt.datetime, tz_name: str) -> str:
    """The 5-field cron that fires at `when`, read in `tz_name`.

    Stored so a runner predating `run_once_at` fires a one-off unchanged: it only
    ever evaluates `cron` against `fire_after`. The expression repeats yearly, and
    that is safe precisely because `fire_after` is pinned a minute before `when`
    (serialize_schedule) and the schedule disables itself on firing
    (services.fire_schedule) — the next year's slot is never due before then."""
    local = when.astimezone(ZoneInfo(tz_name))
    return f"{local.minute} {local.hour} {local.day} {local.month} *"


def _apply_timing(fields: dict, *, current: AgentSchedule | None = None) -> dict:
    """Reconcile cron / run_once_at / timezone in a create or update payload.

    Exactly one of cron and run_once_at describes WHEN. A one-off's cron is
    derived, never supplied; supplying a cron to an existing one-off converts it
    back to recurring. `now` is read here so every surface agrees on "the past"."""
    fields = dict(fields)
    cron = fields.pop("cron", None)
    once = fields.pop("run_once_at", None)
    if cron and once:
        raise InvalidSchedule("give either cron (recurring) or run_once_at (one-off), not both")
    if current is None and not (cron or once):
        raise InvalidSchedule("a schedule needs cron (recurring) or run_once_at (one-off)")
    tz_name = fields.get("timezone") or (current.timezone if current else "UTC")
    validate_timezone(tz_name)
    if cron:
        fields["cron"] = cron
        fields["run_once_at"] = None
    elif once:
        if once.tzinfo is None:
            # No offset means wall-clock time where the schedule lives:
            # "2026-09-28T09:00" in America/Denver is 9am Denver.
            once = once.replace(tzinfo=ZoneInfo(tz_name))
        # Cron has minute resolution, so a one-off does too.
        once = once.replace(second=0, microsecond=0)
        if once <= timezone.now():
            raise InvalidSchedule(f"run_once_at {once.isoformat()} is not in the future")
        fields["run_once_at"] = once
        fields["cron"] = one_off_cron(once, tz_name)
        # Re-arming a one-off that already fired (it disabled itself) must make it
        # fire again — unless the caller said otherwise in the same request.
        fields.setdefault("enabled", True)
    elif current is not None and current.run_once_at and "timezone" in fields:
        # Same instant, re-expressed in the new zone.
        fields["cron"] = one_off_cron(current.run_once_at, tz_name)
    return fields


# The role floor for every schedule WRITE. A schedule is prompt text the runner
# later executes as the agent, holding the agent's credentials — the same
# reshaping tier as apps/agents/api.py::_agent_for_write, which is where the
# rest of that surface's writes already sit. Reads (list, preview) stay at bare
# membership: seeing when your team's agent runs is interaction, not authorship.
WRITE_ROLE = wsvc.WorkspaceMembership.EDITOR


def _resolve_agent(
    user,
    agent_slug: str,
    *,
    workspace_slug: str | None = None,
    require_role: str | None = None,
) -> Agent:
    """Resolve an agent, gated by workspace membership. Request-free twin of
    apps/harness/api.py::_agent_or_404 — the tenant-URL pin is a parameter.

    REST passes request.workspace_slug (preserving today's behavior); the MCP
    passes None (membership gating only, no tenant-URL concept). Resolution
    failures raise ScheduleNotFound — 404-not-403 on the REST side, no existence
    leak.

    Membership is checked UNCONDITIONALLY. This read `if agent.workspace_id and
    not is_member(...)`, which short-circuits to "allow" on a workspace-less
    agent — handing every authenticated caller (REST and MCP alike) that agent's
    full schedule CRUD: list, create, update, delete, run-now. `Agent.workspace`
    is NOT NULL as of agents/0013 so no such row can exist, but the fail-open
    SHAPE is the bug that kept recurring, so it goes too.

    `require_role` is the role floor, and it lives HERE rather than in the Ninja
    handlers on purpose. A schedule is arbitrary prompt text that the runner
    later executes AS the agent, with the agent's resolved credentials — and
    `run-now` means "immediately". So schedule CRUD is the same reshaping tier
    as `apps/agents/api.py::_agent_for_write`, not the interaction tier: bare
    membership let a `viewer` write a prompt of their choosing and fire it. It
    is enforced in the shared resolver because the MCP tools
    (`create_schedule`, `update_schedule`, `delete_schedule`,
    `run_schedule_now`) reach these services without passing through a Ninja
    handler at all — a gate in the handlers would be a gate with the MCP
    surface walking around it.

    Ordering is resolve-then-authorize, matching `_agent_for_write`: a
    non-member still gets ScheduleNotFound (404), and only a member who is
    genuinely under-privileged sees ScheduleForbidden (403)."""
    agent = Agent.objects.filter(slug=agent_slug).first()
    if agent is None:
        raise ScheduleNotFound(agent_slug)
    if workspace_slug and agent.workspace_id != workspace_slug:
        raise ScheduleNotFound(agent_slug)  # wrong tenant
    if not agent.workspace_id or not wsvc.is_member(user, agent.workspace_id):
        raise ScheduleNotFound(agent_slug)
    if require_role and not wsvc.has_role_at_least(user, agent.workspace_id, require_role):
        raise ScheduleForbidden(agent_slug, required=require_role)
    return agent


def _resolve_schedule(
    user,
    agent_slug: str,
    schedule_id: int,
    *,
    workspace_slug: str | None = None,
    require_role: str | None = None,
) -> AgentSchedule:
    agent = _resolve_agent(
        user, agent_slug, workspace_slug=workspace_slug, require_role=require_role
    )
    schedule = AgentSchedule.objects.filter(pk=schedule_id, agent=agent).first()
    if schedule is None:
        raise ScheduleNotFound(f"{agent_slug}/{schedule_id}")
    return schedule


def serialize_schedule(schedule: AgentSchedule) -> dict:
    """The single serialized shape. REST builds ScheduleOut(**this); the MCP
    tools return it directly. fire_after = last_slot or created_at is the anchor
    the runner passes to due_slot — last_slot is NULL until the first fire, and
    an unbounded backward lookup would fire a slot predating the schedule."""
    latest = services.latest_occurrence_turn(schedule)
    now = timezone.now()
    fire_after = schedule.last_slot or schedule.created_at
    if schedule.run_once_at:
        # Pin the anchor just before the one instant, so the derived cron's
        # earlier-year matches can never be due (see one_off_cron).
        fire_after = max(fire_after, schedule.run_once_at - dt.timedelta(minutes=1))
        pending = schedule.enabled and schedule.run_once_at > now
        next_runs = [schedule.run_once_at] if pending else []
    else:
        next_runs = next_slots(schedule.cron, schedule.timezone, now=now, count=3)
    return {
        "id": schedule.id,
        "agent_slug": schedule.agent_slug,
        "name": schedule.name,
        "prompt": schedule.prompt,
        "cron": schedule.cron,
        "run_once_at": schedule.run_once_at,
        "timezone": schedule.timezone,
        "enabled": schedule.enabled,
        "routing": schedule.routing,
        "grace_minutes": schedule.grace_minutes,
        "always_run": schedule.always_run,
        "notify": schedule.notify,
        "last_slot": schedule.last_slot,
        "fire_after": fire_after,
        "next_runs": next_runs,
        "last_status": latest.status if latest else "",
        "created_by_email": schedule.created_by.email if schedule.created_by_id else None,
        "created_at": schedule.created_at,
        "updated_at": schedule.updated_at,
    }


def list_schedules(user, agent_slug: str, *, workspace_slug: str | None = None) -> list[AgentSchedule]:
    agent = _resolve_agent(user, agent_slug, workspace_slug=workspace_slug)
    return list(agent.schedules.all())


def create_schedule(
    user, agent_slug: str, fields: dict, *, workspace_slug: str | None = None
) -> AgentSchedule:
    agent = _resolve_agent(
        user, agent_slug, workspace_slug=workspace_slug, require_role=WRITE_ROLE
    )
    creator = user if getattr(user, "is_authenticated", False) else None
    fields = _apply_timing(fields)
    try:
        # Own savepoint: an IntegrityError from uniq_agent_schedule_name must not
        # poison the request transaction (SESSION_SAVE_EVERY_REQUEST would then
        # 400 instead of surfacing the 409). Mirrors apps/projects/api.py.
        with transaction.atomic():
            return AgentSchedule.objects.create(
                agent=agent, created_by=creator, updated_by=creator, **fields
            )
    except IntegrityError:
        raise DuplicateScheduleName(fields["name"]) from None


def update_schedule(
    user, agent_slug: str, schedule_id: int, fields: dict, *, workspace_slug: str | None = None
) -> AgentSchedule:
    schedule = _resolve_schedule(
        user, agent_slug, schedule_id, workspace_slug=workspace_slug, require_role=WRITE_ROLE
    )
    fields = _apply_timing(fields, current=schedule)
    for key, value in fields.items():
        setattr(schedule, key, value)
    if fields:
        schedule.updated_by = user if getattr(user, "is_authenticated", False) else None
        try:
            with transaction.atomic():  # savepoint — see create_schedule
                schedule.save()
        except IntegrityError:
            raise DuplicateScheduleName(schedule.name) from None
    return schedule


def delete_schedule(
    user, agent_slug: str, schedule_id: int, *, workspace_slug: str | None = None
) -> None:
    """Retire open occurrences FIRST — see the module docstring and the spec.
    There is no Turn->AgentSchedule FK, so nothing cascades; an executing
    occurrence would otherwise hold one_executing_turn_per_agent forever."""
    schedule = _resolve_schedule(
        user, agent_slug, schedule_id, workspace_slug=workspace_slug, require_role=WRITE_ROLE
    )
    services.supersede_open_turns(schedule, reason="schedule deleted")
    schedule.delete()


def run_schedule_now(
    user, agent_slug: str, schedule_id: int, *, workspace_slug: str | None = None
) -> AgentSchedule:
    schedule = _resolve_schedule(
        user, agent_slug, schedule_id, workspace_slug=workspace_slug, require_role=WRITE_ROLE
    )
    services.run_schedule_now(schedule)
    return schedule


def week_schedules(workspace_ids: set, start: dt.datetime, *, created_by=None) -> list[dict]:
    """Every ENABLED schedule in `workspace_ids`, each with its fires in the
    week [start, start+8d). `workspace_ids` is the caller's already-resolved
    visible-workspace set (the route computes it from apps.workspaces.services),
    so this stays request-free.

    The set used to be allowed to contain `None`, meaning "also match legacy
    unhomed agents", which this turned into an OR'd
    `Q(agent__workspace_id__isnull=True)` — leaking an unhomed agent's schedule,
    cron and timezone to every authenticated flat-route caller. `Agent.workspace`
    is NOT NULL as of agents/0013, so there is no such agent and no such leg:
    membership is the whole rule.

    `created_by` (a User) narrows to schedules that person set up — this is what
    makes 'my calendar' actually personal, rather than 'every schedule in my
    workspaces'."""
    # 8 days, not 7, because `start` is a fixed UTC instant (local-Monday-
    # midnight) but the grid renders 7 LOCAL calendar days — which is up to 169h
    # on a fall-back week (a 25h local day) and only 167h on spring-forward. A
    # fixed 7*24h window would drop a fire in the final local hour of Sunday on a
    # fall-back week (it lands in the Sunday column but past the 168h cutoff). One
    # extra day of slack absorbs the ±1h DST wobble with margin; the client's own
    # `dayIdx < 7` guard (bucketByDay) trims any fire that overflows into day 7,
    # so widening the window never double-counts or shows next week's fires.
    end = start + dt.timedelta(days=8)
    schedules = (
        AgentSchedule.objects.filter(enabled=True)
        .filter(agent__workspace_id__in=workspace_ids)
        .select_related("agent")
    )
    if created_by is not None:
        schedules = schedules.filter(created_by=created_by)
    rows = []
    for s in schedules:
        rows.append({
            "schedule": serialize_schedule(s),
            "workspace_slug": s.agent.workspace_id,
            "fires": (
                [s.run_once_at] if start <= s.run_once_at < end else []
            ) if s.run_once_at else slots_between(s.cron, s.timezone, start=start, end=end),
        })
    return rows


def preview_cron(
    user, agent_slug: str, cron: str, timezone_name: str, *, workspace_slug: str | None = None
) -> list[dt.datetime]:
    """agent_slug is for authorization only (you must see the agent to preview
    against it) — matches the REST preview route, which resolves + ignores it."""
    _resolve_agent(user, agent_slug, workspace_slug=workspace_slug)
    return next_slots(cron, timezone_name, now=timezone.now(), count=3)
