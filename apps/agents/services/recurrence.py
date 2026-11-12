"""Recurring tasks: a TEMPLATE task with a rhythm (`AgentTask.recurrence`) and the
INSTANCES made of it, one per period.

`spawn` is the one writer of instances, idempotent per (template, period): the period is
stamped on the instance (`template_period`, unique per template), so a second spawn for the
same period returns the first, and moving an instance's `due` changes neither. `spawn_due`
is the tick (`AgentsConfig.ready`): each `auto` template gets its current period's instance
once that period has begun, in the workspace's timezone (`TaskSettings.timezone`).

A period begins: every day (`day`), on Monday (`week`, and `2weeks` on alternate Mondays
counted from the template's first instance, else from the week it was made), on the 1st
(`month`). `as_needed` has no rhythm: it is spawned by hand, one instance per date, and
never by the tick. A template made part-way through a period gets its first automatic
instance at the next one (`spawn` makes this one by hand).
"""
from __future__ import annotations

import datetime as dt
import logging
import zoneinfo

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from .. import work
from ..models import Agent, AgentTask, TaskEvent, TaskSettings
from .projects import _link_participants
from .tasks import _refuse_outsiders

logger = logging.getLogger(__name__)

DAY, WEEK, TWO_WEEKS, MONTH, AS_NEEDED = "day", "week", "2weeks", "month", "as_needed"
EVERY = (DAY, WEEK, TWO_WEEKS, MONTH, AS_NEEDED)
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def clean(value) -> dict | None:
    """`value` as stored (`{"every", "weekday"?, "auto"}`), or None (not recurring).
    Raises ValueError naming what is wrong."""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    if not isinstance(value, dict):
        raise ValueError("recurrence is an object {every, weekday?, auto?} or null")
    extra = set(value) - {"every", "weekday", "auto"}
    if extra:
        raise ValueError(f"recurrence takes every, weekday and auto, not {', '.join(sorted(extra))}")
    every = value.get("every")
    if every not in EVERY:
        raise ValueError(f"recurrence every must be one of {'|'.join(EVERY)}, got {every!r}")
    auto = value.get("auto", False)
    if not isinstance(auto, bool):
        raise ValueError("recurrence auto is true or false")
    if every == AS_NEEDED and auto:
        raise ValueError("an as_needed task is spawned by hand; it cannot be auto")
    out: dict = {"every": every, "auto": auto}
    weekday = value.get("weekday")
    if weekday is not None:
        if every not in (WEEK, TWO_WEEKS):
            raise ValueError("recurrence weekday is for every week or 2weeks only")
        if weekday not in WEEKDAYS:
            raise ValueError(f"recurrence weekday is one of {'|'.join(WEEKDAYS)}, got {weekday!r}")
        out["weekday"] = weekday
    return out


# ---- periods -------------------------------------------------------------------


def tz_of(workspace_slug: str) -> zoneinfo.ZoneInfo:
    name = TaskSettings.objects.filter(workspace_id=workspace_slug).values_list("timezone", flat=True).first()
    try:
        return zoneinfo.ZoneInfo(name or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo("UTC")


def _monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _anchor(template: AgentTask) -> dt.date | None:
    """The Monday a `2weeks` template alternates from once it has instances: its first
    instance's period. None before then (the tick counts from the week it was made; a
    first instance spawned by hand sets the anchor to its own week)."""
    return (template.instances.exclude(template_period__isnull=True).order_by("id")
            .values_list("template_period", flat=True).first())


def _created_monday(template: AgentTask, tz) -> dt.date:
    return _monday(timezone.localtime(template.created_at, tz).date())


def period_of(template: AgentTask, day: dt.date, *, anchor: dt.date | None = None) -> dt.date:
    """The first day of the period `day` falls in, for this template."""
    every = template.recurrence["every"]
    if every == WEEK:
        return _monday(day)
    if every == TWO_WEEKS:
        base = anchor or _anchor(template) or _monday(day)
        monday = _monday(day)
        return monday - dt.timedelta(weeks=((monday - base).days // 7) % 2)
    if every == MONTH:
        return day.replace(day=1)
    return day  # day, as_needed


def due_in(template: AgentTask, period: dt.date) -> dt.date:
    """When an instance for the period starting `period` is due: its weekday that week
    (Monday when none is set), else the period's first day."""
    weekday = template.recurrence.get("weekday")
    if template.recurrence["every"] in (WEEK, TWO_WEEKS) and weekday:
        return period + dt.timedelta(days=WEEKDAYS.index(weekday))
    return period


# ---- the writer -----------------------------------------------------------------


@transaction.atomic
def spawn(template: AgentTask, due: dt.date, by=None) -> tuple[AgentTask, bool]:
    """The instance of `template` for the period `due` falls in: made (due `due`, title,
    notes, assignee, project and labels copied, status todo, relations not), or the one
    already there. Returns (instance, made). `by` is the person or agent who asked, or
    None for the tick. Raises ValueError on a task that is not a template, and
    `AssigneeError` when the copied assignee may no longer have work in its project."""
    template = AgentTask.objects.select_for_update(of=("self",)).select_related("project").get(pk=template.pk)
    if template.recurrence is None:
        raise ValueError(f"{template.key} is not a recurring task; set its recurrence first")
    period = period_of(template, due)
    existing = template.instances.filter(template_period=period).first()
    if existing is not None:
        return existing, False
    _refuse_outsiders(template.project, agent=template.agent, people=(template.assignee_user,))
    by_user = by if getattr(by, "is_authenticated", False) else None
    by_agent = by if isinstance(by, Agent) else None
    try:
        with transaction.atomic():  # savepoint: a racing spawn for the same period wins
            instance = AgentTask.objects.create(
                workspace_id=template.workspace_id, title=template.title, notes=template.notes,
                agent=template.agent, assignee_user=template.assignee_user, project=template.project,
                priority=template.priority, status=AgentTask.TODO, due=due, template=template,
                template_period=period, created_by_user=by_user, created_by_agent=by_agent,
                rank=work.top_rank(template.workspace_id),
            )
            labels = list(template.labels.all())
            if labels:
                instance.labels.set(labels)
            work.record_created(instance, by_user=by_user, by_agent=by_agent)
    except IntegrityError:
        found = template.instances.filter(template_period=period).first()
        if found is None:
            raise
        return found, False
    _link_participants(instance)
    return instance, True


# ---- last_done ---------------------------------------------------------------


def annotate_last_done(tasks) -> list:
    """Cache `last_done` on every template among `tasks`, in one query."""
    tasks = list(tasks)
    ids = [t.pk for t in tasks if t.recurrence is not None]
    if not ids:
        return tasks
    done = dict(TaskEvent.objects.filter(task__template_id__in=ids, task__status=AgentTask.DONE,
                                         kind=TaskEvent.FIELD, field="status", new=AgentTask.DONE)
                .values("task__template_id").annotate(at=Max("created_at")).values_list("task__template_id", "at"))
    for t in tasks:
        if t.recurrence is not None:
            t._last_done = done.get(t.pk)
    return tasks


# ---- the tick -------------------------------------------------------------------


def _due_now(template: AgentTask, now: dt.datetime) -> dt.date | None:
    """The due date of the instance the tick should have made by `now`, or None: the
    current period's, once that period began after the template was made."""
    tz = tz_of(template.workspace_id)
    today = timezone.localtime(now, tz).date()
    anchor = None
    if template.recurrence["every"] == TWO_WEEKS:
        anchor = _anchor(template) or _created_monday(template, tz)
    period = period_of(template, today, anchor=anchor)
    if period < timezone.localtime(template.created_at, tz).date():
        return None
    return due_in(template, period)


def spawn_due(now: dt.datetime | None = None) -> int:
    """The tick: each `auto` template's current instance, where it is missing. One failing
    template is logged and the rest still run. Returns how many were made."""
    now = now or timezone.now()
    made = 0
    templates = (AgentTask.objects.filter(recurrence__isnull=False, recurrence__auto=True)
                 .select_related("project", "agent", "assignee_user").order_by("id"))
    for template in templates:
        try:
            due = _due_now(template, now)
            if due is not None and spawn(template, due)[1]:
                made += 1
        except Exception:  # noqa: BLE001 — one template must not stop the rest
            logger.exception("recurring task %s could not spawn", template.key)
    return made
