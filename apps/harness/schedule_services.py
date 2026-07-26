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

from canopy_cron import next_slots, slots_between
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


class DuplicateScheduleName(Exception):
    """The uniq_agent_schedule_name constraint was violated on create/update."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _resolve_agent(user, agent_slug: str, *, workspace_slug: str | None = None) -> Agent:
    """Resolve an agent, gated by workspace membership. Request-free twin of
    apps/harness/api.py::_agent_or_404 — the tenant-URL pin is a parameter.

    REST passes request.workspace_slug (preserving today's behavior); the MCP
    passes None (membership gating only, no tenant-URL concept). Every failure
    raises ScheduleNotFound — 404-not-403 on the REST side, no existence leak.

    Membership is checked UNCONDITIONALLY. This read `if agent.workspace_id and
    not is_member(...)`, which short-circuits to "allow" on a workspace-less
    agent — handing every authenticated caller (REST and MCP alike) that agent's
    full schedule CRUD: list, create, update, delete, run-now. `Agent.workspace`
    is NOT NULL as of agents/0013 so no such row can exist, but the fail-open
    SHAPE is the bug that kept recurring, so it goes too."""
    agent = Agent.objects.filter(slug=agent_slug).first()
    if agent is None:
        raise ScheduleNotFound(agent_slug)
    wsvc.auto_join_workspaces(user)
    if workspace_slug and agent.workspace_id != workspace_slug:
        raise ScheduleNotFound(agent_slug)  # wrong tenant
    if not agent.workspace_id or not wsvc.is_member(user, agent.workspace_id):
        raise ScheduleNotFound(agent_slug)
    return agent


def _resolve_schedule(
    user, agent_slug: str, schedule_id: int, *, workspace_slug: str | None = None
) -> AgentSchedule:
    agent = _resolve_agent(user, agent_slug, workspace_slug=workspace_slug)
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
    return {
        "id": schedule.id,
        "agent_slug": schedule.agent_slug,
        "name": schedule.name,
        "prompt": schedule.prompt,
        "cron": schedule.cron,
        "timezone": schedule.timezone,
        "enabled": schedule.enabled,
        "routing": schedule.routing,
        "grace_minutes": schedule.grace_minutes,
        "notify": schedule.notify,
        "last_slot": schedule.last_slot,
        "fire_after": schedule.last_slot or schedule.created_at,
        "next_runs": next_slots(schedule.cron, schedule.timezone, now=timezone.now(), count=3),
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
    agent = _resolve_agent(user, agent_slug, workspace_slug=workspace_slug)
    creator = user if getattr(user, "is_authenticated", False) else None
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
    schedule = _resolve_schedule(user, agent_slug, schedule_id, workspace_slug=workspace_slug)
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
    schedule = _resolve_schedule(user, agent_slug, schedule_id, workspace_slug=workspace_slug)
    services.supersede_open_turns(schedule, reason="schedule deleted")
    schedule.delete()


def run_schedule_now(
    user, agent_slug: str, schedule_id: int, *, workspace_slug: str | None = None
) -> AgentSchedule:
    schedule = _resolve_schedule(user, agent_slug, schedule_id, workspace_slug=workspace_slug)
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
            "fires": slots_between(s.cron, s.timezone, start=start, end=end),
        })
    return rows


def preview_cron(
    user, agent_slug: str, cron: str, timezone_name: str, *, workspace_slug: str | None = None
) -> list[dt.datetime]:
    """agent_slug is for authorization only (you must see the agent to preview
    against it) — matches the REST preview route, which resolves + ignores it."""
    _resolve_agent(user, agent_slug, workspace_slug=workspace_slug)
    return next_slots(cron, timezone_name, now=timezone.now(), count=3)
