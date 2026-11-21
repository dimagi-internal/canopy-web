"""Where a learner runs (`BrainTarget`), and how a target's schedule starts a run.

A target is a workspace (a run reads eligible sources of it and the workspaces below it) or a workspace project (the
sessions `core`-tied to it, in its workspace's subtree). A `system` learner takes either; a `project` learner takes
projects only (what it makes is for the project's readers); the per-person learners and the built-in project readers
take none. A learner with no target never runs.

Each target owns a normal canopy schedule on the learner's operator agent (`harness.AgentSchedule`): its cron, Run now
and history are where the agent's other schedules are, and canopy's own clock fires it (`harness.schedule_clock`, or a
laptop runner reporting the slot). The fire does not enqueue a plain turn: the harness asks this module's announcer
(`harness.schedule_hooks`), which starts a brain run (`runs.start`) only when one may start and there is something new
for this target (`reading.candidates`), one per slot and one open per target. The schedule is created by the learner's
accountable admin's authority (`registry.accountable`), so it fires as their standing request, like any schedule; whoever
added the target is recorded on it and in the setting's audit.

Each target's runs, what they read and what they found are its own: a workspace target's run is about `system` and that
workspace, a project target's about the project, scoped to its workspace (`subject_of`)."""
from __future__ import annotations

import logging

from canopy_cron import validate_cron, validate_timezone
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.agents.models import AgentProject
from apps.harness import schedule_hooks
from apps.harness.models import AgentSchedule
from apps.workspaces.models import Workspace

from . import budget, reading, readers, registry, runs
from .models import PROJECT, SYSTEM, BrainCapability, BrainRun, BrainTarget

logger = logging.getLogger(__name__)

#: A new target's schedule when none is given: daily at 06:00 UTC.
DEFAULT_CRON, DEFAULT_TZ = "0 6 * * *", "UTC"
#: How long a run's turn may hold the operator before its schedule releases it (`AgentSchedule.grace_minutes`), as
#: long as a run may stay open (`runs.DEFAULT_TIMEOUT_SECONDS`).
GRACE_MINUTES = runs.DEFAULT_TIMEOUT_SECONDS // 60


class TargetRefusedError(ValueError):
    """The target asked for may not be added (a 422 at the route), with the reason in words."""


def label(target: BrainTarget) -> str:
    return f"{target.workspace_id}/{target.project.handle}" if target.project_id else target.workspace_id


def subject_of(target: BrainTarget) -> tuple[AgentProject | None, str]:
    """`(subject, scope)` a run for this target is started with: the project (or None for a workspace target) and the
    workspace it reads (with the ones below it)."""
    return (target.project if target.project_id else None), target.workspace_id


def kind_refusal(cap: BrainCapability, project: AgentProject | None) -> str:
    """Why `cap` may not take this kind of target, or ""."""
    if cap.slug == readers.PROJECT_READERS:
        return "the project readers are built in and never run"
    if cap.purpose_class == SYSTEM:
        return ""
    if cap.purpose_class == PROJECT:
        return "" if project is not None else f"{cap.slug} learns for a project's readers: aim it at a project"
    return f"{cap.slug} learns about one person; it takes no target"


def _schedule_name(cap: BrainCapability, workspace: Workspace, project: AgentProject | None) -> str:
    where = f"{workspace.slug}/{project.handle}" if project is not None else workspace.slug
    return f"Learning: {cap.name} for {where}"[:200]


def add(cap: BrainCapability, *, workspace: Workspace, project: AgentProject | None = None, by,
        cron: str = DEFAULT_CRON, tz: str = DEFAULT_TZ) -> BrainTarget:
    """Aim `cap` at `workspace` (or at `project`, a workspace project of it) on a schedule of its own. The caller has
    already checked `by` may manage learning there and that the operator lives there or above (`api_manage`)."""
    why = kind_refusal(cap, project)
    if why:
        raise TargetRefusedError(why)
    if project is not None and (project.is_personal or project.workspace_id != workspace.slug):
        raise TargetRefusedError("aim it at a workspace project of that workspace")
    if cap.operator_id is None:
        raise TargetRefusedError(f"{cap.slug} has no operator agent; give it one first")
    owner = registry.accountable(cap)
    if owner is None:
        raise TargetRefusedError(f"nobody active answers for {cap.slug}; enable it first")
    try:
        cron, tz = validate_cron(cron.strip()), tz.strip() or DEFAULT_TZ
        validate_timezone(tz)
    except ValueError as e:
        raise TargetRefusedError(str(e)) from e
    try:
        with transaction.atomic():
            schedule = AgentSchedule.objects.create(
                agent=cap.operator, created_by=owner, updated_by=by, name=_schedule_name(cap, workspace, project),
                prompt=cap.entry, cron=cron, timezone=tz, grace_minutes=GRACE_MINUTES, always_run=True)
            return BrainTarget.objects.create(capability=cap, workspace=workspace, project=project,
                                              schedule=schedule, created_by=by)
    except IntegrityError as e:
        raise TargetRefusedError(f"{cap.slug} is already aimed there") from e


def set_budget(target: BrainTarget, **changes) -> BrainTarget:
    """Change the target's budget (`budget.FIELDS`); `max_tokens_per_day` None removes the token cap."""
    unknown = set(changes) - set(budget.FIELDS)
    if unknown:
        raise TargetRefusedError(f"no such budget: {', '.join(sorted(unknown))}")
    for field, value in changes.items():
        setattr(target, field, value)
    target.save(update_fields=list(changes))
    return target


def remove(target: BrainTarget) -> None:
    """Take the target away: its schedule goes with it (deleting either deletes both), and a run still open for it
    is left to finish."""
    AgentSchedule.objects.filter(pk=target.schedule_id).delete()


def operator_refusal(cap: BrainCapability, agent) -> str:
    """Why `cap`'s operator may not become `agent` (None: no operator) given its targets, or "": a learner with targets
    keeps an operator, and every target's workspace must be the new one's or below it (`run_place_refusal`)."""
    targets = list(cap.targets.all())
    if not targets:
        return ""
    if agent is None:
        return f"{cap.slug} has {len(targets)} target(s); remove them before taking its operator away"
    probe = BrainCapability(slug=cap.slug, operator=agent)
    bad = sorted(t.workspace_id for t in targets if run_place_refusal(probe, t.workspace_id))
    return (f"{agent.slug} lives outside {', '.join(bad)}, where {cap.slug} has targets; it could not read there"
            if bad else "")


def follow(cap: BrainCapability) -> int:
    """The learner changed: its targets' schedules follow its operator and entry, and fire as whoever now answers
    for it (`registry.accountable`), the authority its runs act with."""
    if cap.operator_id is None:
        return 0
    owner = registry.accountable(cap)
    fields = {"agent_id": cap.operator_id, "prompt": cap.entry, **({"created_by": owner} if owner else {})}
    return AgentSchedule.objects.filter(brain_target__capability=cap).update(**fields)


def _record(target: BrainTarget, kind: str, summary: str, **payload) -> None:
    from apps.events import services as events  # noqa: PLC0415 — as the other event writers of this app

    try:
        events.record([{"source": "brain.target", "kind": kind, "summary": summary[:500],
                        "key": f"brain-target:{target.pk}:{kind}",
                        "payload": {"target": target.pk, "capability": target.capability.slug, **payload}}],
                      workspace=target.workspace)
    except Exception:  # noqa: BLE001 — a log line never fails a fire
        logger.exception("could not record %s for target %s", kind, target.pk)


def run_place_refusal(cap: BrainCapability, ws: str) -> str:
    """Why a run of `cap` cannot be started for `ws` because of where its operator lives, or "": the operator must be
    an agent of `ws` or of a workspace above it, so a run's conversation never holds a workspace's words in a sibling's
    or a sub-workspace's agent."""
    if cap.operator_id is None:
        return ""
    home = Workspace.objects.filter(slug=ws).first()
    above = {ws, *(home.ancestor_slugs() if home is not None else [])}
    if cap.operator.workspace_id not in above:
        return f"{cap.slug}'s operator lives outside {ws} and the workspaces above it; it cannot read {ws}"
    return ""


def why_not(target: BrainTarget, *, accountable=None) -> str:
    """Why a run for this target cannot start now, or "": the run's own refusals (learning reads, enabled, operator,
    entry, accountable, one open), where its operator lives, and its budget for the day (`budget.start_refusal`)."""
    cap = target.capability
    subject, scope = subject_of(target)
    if subject is not None and (subject.is_personal or subject.status != AgentProject.ACTIVE):
        return f"{subject.handle} is not an active workspace project"
    return (kind_refusal(cap, subject) or run_place_refusal(cap, scope)
            or runs.refusal(cap, subject, scope=scope, accountable=accountable) or budget.start_refusal(target))


def has_new(target: BrainTarget) -> bool:
    """Is there an eligible source of this target no clean run of it read yet."""
    cap = target.capability
    subject, scope = subject_of(target)
    subject_type, subject_id = runs.subject_of(cap, subject, scope)
    probe = BrainRun(capability=cap, subject_type=subject_type, subject=subject_id, purpose=(cap.purposes or [""])[0],
                     floor=0, scope=scope)
    return reading.candidates(probe).exists()


def start(target: BrainTarget, *, slot=None, accountable=None) -> tuple[BrainRun | None, str]:
    """Start a run for this target if one may start and there is something new: `(run, "")`, else `(None, why)`."""
    runs.abandon_stale()
    accountable = accountable or target.schedule.created_by or registry.accountable(target.capability)
    why = why_not(target, accountable=accountable)
    if not why and not has_new(target):
        why = "nothing new to learn from"
    if why:
        return None, why
    subject, scope = subject_of(target)
    try:
        return runs.start(target.capability, subject=subject, scope=scope, accountable=accountable, target=target,
                          slot=slot), ""
    except runs.StartRefusedError as e:
        return None, str(e)


def announce(schedule: AgentSchedule, slot, *, manual: bool = False) -> tuple[BrainRun | None, bool]:
    """The target's schedule fired (`slot`), or its Run now was pressed (`manual`). Consumes the slot whatever comes
    of it, so no firer reports it again; starts at most one run per slot."""
    target = (BrainTarget.objects.select_related("capability", "capability__operator", "project", "workspace",
                                                 "schedule__created_by").get(schedule=schedule))
    if not manual:
        with transaction.atomic():
            if schedule.last_slot is None or slot > schedule.last_slot:
                AgentSchedule.objects.filter(pk=schedule.pk).update(last_slot=slot, updated_at=timezone.now())
                schedule.last_slot = slot
        done = BrainRun.objects.filter(target=target, slot=slot).first()
        if done is not None:
            return done, False
    run, why = start(target, slot=None if manual else slot)
    if run is None:
        _record(target, "target.skipped", f"{target.capability.slug} for {label(target)} did not run: {why}",
                slot=slot.isoformat() if slot else None, manual=manual)
        return None, False
    _record(target, "target.run", f"{target.capability.slug} for {label(target)}: run {run.pk} started",
            run=run.pk, manual=manual)
    return run, True


def run_now(target: BrainTarget, *, by) -> BrainRun | None:
    """Run now from the brain page: the target's schedule's own Run now, as its creator's standing request with `by`
    recorded as who pressed it. Raises `TargetRefusedError` with why when no run starts."""
    from apps.harness import schedule_turns, triggers  # noqa: PLC0415 — harness's firing path

    why = why_not(target, accountable=target.schedule.created_by)
    if why:
        raise TargetRefusedError(why)
    if not has_new(target):
        raise TargetRefusedError("nothing new to learn from")
    try:
        schedule_turns.run_schedule_now(target.schedule, clicked_by=by)
    except triggers.TriggerRefused as e:
        raise TargetRefusedError(f"its schedule did not fire: {e.reason}") from e
    run = BrainRun.objects.filter(target=target).order_by("-pk").first()
    if run is None or run.state != BrainRun.OPEN:
        raise TargetRefusedError("the run did not start; the brain page's events say why")
    return run


class _Announcer:
    def owns(self, schedule) -> bool:
        return BrainTarget.objects.filter(schedule_id=schedule.pk).exists()

    def announce(self, schedule, slot, *, manual: bool = False):
        return announce(schedule, slot, manual=manual)

    def run_for(self, schedule, slot):
        return BrainRun.objects.filter(target__schedule=schedule, slot=slot).first()


def register() -> None:
    schedule_hooks.register(_Announcer())
