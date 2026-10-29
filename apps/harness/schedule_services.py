"""Request-free schedule service layer.

The MCP invariant (apps/mcp/api_tools.py: every tool IS its REST route) is that tools call the SAME
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
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import clis, schedule_turns, triggers
from .models import AgentSchedule
from .schedule_turns import fire_after_of


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


class TriggerStopped(Exception):
    """Run now on a trigger whose creator lost the right to it (`triggers.refusal`)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    kind = fields.get("kind") or (current.kind if current else AgentSchedule.CRON)
    if kind == AgentSchedule.INBOUND_MAIL:
        # Mail fires it, not a clock: no cron, no instant.
        if cron or once:
            raise InvalidSchedule("an inbound_mail trigger has no cron or run_once_at")
        return _apply_mail(fields, current=current)
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


def _apply_mail(fields: dict, *, current: AgentSchedule | None) -> dict:
    """Validate an inbound_mail trigger's sender binding (`triggers`)."""
    if "sender_address" in fields or current is None:
        try:
            fields["sender_address"] = triggers.normalize_address(fields.get("sender_address") or "")
        except triggers.InvalidTrigger as exc:
            raise InvalidSchedule(str(exc)) from None
    if "subject_pattern" in fields:
        try:
            fields["subject_pattern"] = triggers.validate_pattern(fields["subject_pattern"])
        except triggers.InvalidTrigger as exc:
            raise InvalidSchedule(str(exc)) from None
    if current is None:
        fields["cron"] = ""
        fields["run_once_at"] = None
    return fields


def _apply_kind(fields: dict, *, current: AgentSchedule | None = None) -> dict:
    """Kind, turn mode and the per-kind shape. A trigger's kind is fixed once made."""
    fields = dict(fields)
    if current is not None:
        if "kind" in fields and fields["kind"] != current.kind:
            raise InvalidSchedule("a trigger's kind cannot change; make a new one")
        fields.pop("kind", None)
        kind = current.kind
    else:
        kind = fields.setdefault("kind", AgentSchedule.CRON)
    if kind not in triggers.KINDS:
        raise InvalidSchedule(f"kind must be one of {', '.join(triggers.KINDS)}")
    if "turn_mode" in fields and (fields["turn_mode"] or "") not in triggers.TURN_MODES:
        raise InvalidSchedule("turn_mode must be manual, auto or blank")
    if kind == AgentSchedule.CRON:
        if fields.get("sender_address") or fields.get("subject_pattern"):
            raise InvalidSchedule("a cron trigger has no sender_address or subject_pattern")
        fields.pop("sender_address", None)
        fields.pop("subject_pattern", None)
        fields.pop("mail_quiet_minutes", None)
        prompt = fields.get("prompt", current.prompt if current else "")
        if not (prompt or "").strip():
            raise InvalidSchedule("a cron trigger needs a prompt")
    return _apply_timing(fields, current=current)


def _check_may_set_up(user, agent, *, kind: str, turn_mode: str) -> None:
    """The gate for every trigger write (`triggers.may_set_up`): `agent.work` for a
    cron trigger, an agent admin for an inbound_mail trigger or an auto one."""
    needed = triggers.may_set_up(user, agent, kind=kind, turn_mode=turn_mode)
    if needed is not None:
        raise ScheduleForbidden(agent.slug, required=needed)


# Every trigger WRITE is gated by `triggers.may_set_up` (`_check_may_set_up`): a
# cron trigger is prompt text the runner later executes as the agent — the
# reshaping tier, `agent.work`, as apps/agents/gates.py::agent_for_work; an
# inbound_mail trigger (an outside sender making the agent work) or an auto one is
# an agent admin's. Reads (list, preview) stay at bare membership: seeing when
# your team's agent runs is interaction, not authorship.


def _resolve_agent(
    user,
    agent_slug: str,
    *,
    workspace_slug: str | None = None,
    require: str | None = None,
) -> Agent:
    """Resolve an agent, gated by workspace membership. Request-free twin of
    apps/agents/gates.py::agent_for_read — the tenant-URL pin is a parameter.

    The scope is `gates.agent_for_read`'s: `wsvc.member_scope(user, pin)`.
    REST passes request.workspace_slug, so a pinned request reaches an agent in
    the pin or in a workspace below it that the caller is in — the same agents
    the agent page under that pin resolves, so its Schedules tab never 404s on
    an agent the page shows. The MCP passes None: every membership. Membership
    is also checked directly, so a pin no middleware vetted still reaches
    nothing the caller is not in. Resolution failures raise ScheduleNotFound —
    404-not-403 on the REST side, no existence leak.

    Membership is checked UNCONDITIONALLY. This read `if agent.workspace_id and
    not is_member(...)`, which short-circuits to "allow" on a workspace-less
    agent — handing every authenticated caller (REST and MCP alike) that agent's
    full schedule CRUD: list, create, update, delete, run-now. `Agent.workspace`
    is NOT NULL as of agents/0013 so no such row can exist, but the fail-open
    SHAPE is the bug that kept recurring, so it goes too.

    `require` is the capability (`apps/workspaces/permissions.py`), and it lives HERE rather than in the Ninja
    handlers on purpose. A schedule is arbitrary prompt text that the runner
    later executes AS the agent, with the agent's resolved credentials — and
    `run-now` means "immediately". So schedule CRUD is the same reshaping tier
    as `apps/agents/gates.py::agent_for_work`, not the interaction tier: bare
    membership let a `viewer` write a prompt of their choosing and fire it. It
    is enforced in the shared resolver because the MCP tools
    (`create_schedule`, `update_schedule`, `delete_schedule`,
    `run_schedule_now`) reach these services without passing through a Ninja
    handler at all — a gate in the handlers would be a gate with the MCP
    surface walking around it.

    Ordering is resolve-then-authorize, matching `agent_for_work`: a
    non-member still gets ScheduleNotFound (404), and only a member who is
    genuinely under-privileged sees ScheduleForbidden (403)."""
    agent = Agent.objects.filter(slug=agent_slug).first()
    if agent is None or not agent.workspace_id:
        raise ScheduleNotFound(agent_slug)
    if agent.workspace_id not in wsvc.member_scope(user, workspace_slug):
        raise ScheduleNotFound(agent_slug)  # wrong tenant / non-member
    if not wsvc.is_member(user, agent.workspace_id):
        raise ScheduleNotFound(agent_slug)
    if require and not perms.can(user, agent.workspace_id, require):
        raise ScheduleForbidden(agent_slug, required=perms.MINIMUM_ROLE[require])
    return agent


def _resolve_schedule(
    user,
    agent_slug: str,
    schedule_id: int,
    *,
    workspace_slug: str | None = None,
    require: str | None = None,
    kind: str | None = None,
) -> AgentSchedule:
    """`kind` narrows the lookup: the /schedules/ routes reach cron triggers only."""
    agent = _resolve_agent(
        user, agent_slug, workspace_slug=workspace_slug, require=require
    )
    qs = AgentSchedule.objects.filter(pk=schedule_id, agent=agent)
    if kind is not None:
        qs = qs.filter(kind=kind)
    schedule = qs.first()
    if schedule is None:
        raise ScheduleNotFound(f"{agent_slug}/{schedule_id}")
    return schedule


def _check_cli(agent, fields: dict) -> None:
    """A schedule's chosen CLI must be one its agent is set up for — refused at
    edit time, like a bad cron, not discovered as a turn that never runs."""
    if (refused := clis.refusal(agent, fields.get("cli") or "")) is not None:
        raise InvalidSchedule(refused)


def serialize_schedule(schedule: AgentSchedule) -> dict:
    """The single serialized shape. REST builds ScheduleOut(**this); the MCP
    tools return it directly. fire_after (`schedule_turns.fire_after_of`) is the
    anchor a runner passes to due_slot — the same one the server's own clock
    (`schedule_clock`) uses, so both pick the same slot."""
    latest = schedule_turns.latest_occurrence_turn(schedule)
    now = timezone.now()
    fire_after = fire_after_of(schedule)
    if schedule.kind != AgentSchedule.CRON:
        next_runs = []
    elif schedule.run_once_at:
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
        "cli": schedule.cli,
        "last_slot": schedule.last_slot,
        "fire_after": fire_after,
        "next_runs": next_runs,
        "last_status": latest.status if latest else "",
        "created_by_email": schedule.created_by.email if schedule.created_by_id else None,
        "created_at": schedule.created_at,
        "updated_at": schedule.updated_at,
    }


def serialize_trigger(trigger: AgentSchedule) -> dict:
    """`serialize_schedule` plus what only a trigger list shows: its kind, its
    sender binding, its own turn mode, and why it is stopped (or None)."""
    return {
        **serialize_schedule(trigger),
        "kind": trigger.kind,
        "sender_address": trigger.sender_address,
        "subject_pattern": trigger.subject_pattern,
        "turn_mode": trigger.turn_mode,
        "mail_quiet_minutes": trigger.mail_quiet_minutes,
        "stopped": triggers.refusal(trigger) if trigger.enabled else None,
        "updated_by_email": trigger.updated_by.email if trigger.updated_by_id else None,
    }


def list_schedules(user, agent_slug: str, *, workspace_slug: str | None = None,
                   kind: str | None = AgentSchedule.CRON) -> list[AgentSchedule]:
    """The agent's triggers — cron only by default (the /schedules/ routes); the
    /triggers/ routes pass `kind=None` for every kind."""
    agent = _resolve_agent(user, agent_slug, workspace_slug=workspace_slug)
    qs = agent.schedules.select_related("agent", "agent__owner", "created_by", "updated_by")
    return list(qs.filter(kind=kind) if kind else qs.all())


def create_schedule(
    user, agent_slug: str, fields: dict, *, workspace_slug: str | None = None
) -> AgentSchedule:
    """Create a trigger (`fields["kind"]`, default cron). Gated by
    `triggers.may_set_up`; the caller becomes its creator, whose authority it fires with."""
    agent = _resolve_agent(user, agent_slug, workspace_slug=workspace_slug)
    creator = user if getattr(user, "is_authenticated", False) else None
    fields = _apply_kind(fields)
    _check_may_set_up(user, agent, kind=fields["kind"], turn_mode=fields.get("turn_mode") or "")
    _check_cli(agent, fields)
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
    user, agent_slug: str, schedule_id: int, fields: dict, *, workspace_slug: str | None = None,
    kind: str | None = AgentSchedule.CRON,
) -> AgentSchedule:
    """Change a trigger. The caller must be able to set up the trigger as it is AND
    as it will be (an editor cannot touch an auto trigger, nor make one). Its
    creator — whose authority it fires with — does not change."""
    schedule = _resolve_schedule(
        user, agent_slug, schedule_id, workspace_slug=workspace_slug, kind=kind
    )
    _check_may_set_up(user, schedule.agent, kind=schedule.kind, turn_mode=schedule.turn_mode)
    fields = _apply_kind(fields, current=schedule)
    _check_may_set_up(user, schedule.agent, kind=schedule.kind,
                      turn_mode=fields.get("turn_mode", schedule.turn_mode) or "")
    _check_cli(schedule.agent, fields)
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
    user, agent_slug: str, schedule_id: int, *, workspace_slug: str | None = None,
    kind: str | None = AgentSchedule.CRON,
) -> None:
    """Retire open occurrences FIRST — see the module docstring and the spec.
    There is no Turn->AgentSchedule FK, so nothing cascades; an executing
    occurrence would otherwise hold one_executing_turn_per_agent forever."""
    schedule = _resolve_schedule(
        user, agent_slug, schedule_id, workspace_slug=workspace_slug, kind=kind
    )
    _check_may_set_up(user, schedule.agent, kind=schedule.kind, turn_mode=schedule.turn_mode)
    schedule_turns.supersede_open_turns(schedule, reason="schedule deleted")
    schedule.delete()


def run_schedule_now(
    user, agent_slug: str, schedule_id: int, *, workspace_slug: str | None = None
) -> AgentSchedule:
    """Fire a cron trigger off-cycle. Mail fires an inbound_mail trigger, nothing else
    (404 here, as for any trigger this route does not reach)."""
    schedule = _resolve_schedule(
        user, agent_slug, schedule_id, workspace_slug=workspace_slug, kind=AgentSchedule.CRON
    )
    _check_may_set_up(user, schedule.agent, kind=schedule.kind, turn_mode=schedule.turn_mode)
    try:
        schedule_turns.run_schedule_now(schedule, clicked_by=user)
    except triggers.TriggerRefused as exc:
        raise TriggerStopped(exc.reason) from None
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
        AgentSchedule.objects.filter(enabled=True, kind=AgentSchedule.CRON)
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
