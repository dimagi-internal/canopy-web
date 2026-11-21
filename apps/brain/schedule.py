"""Starting runs on a schedule, per workspace: one per active project per project feature, one per workspace of its
operator's tree per system feature, only when there is something new.

For each enabled feature that names a `schedule` and an operator, and each of its subjects (`subjects_of`: a `project`
feature's active workspace projects, each in its own workspace; a `system` feature's workspaces, each reading its own
subtree), `due` asks: has a run started in the last interval, is one open, and is there an eligible source in scope no
clean run of the feature read for that subject (`reading.candidates`)? Only then does `run_due` start one. What is new
is per source, not a position, so a session linked, agreed to or backfilled late is still read; a failed run counts
nothing and its sources are read at the next interval, as are the remaining sources of an output a removal left stale
(`RederiveRequest`). A run open past the timeout is abandoned first (`runs.abandon_stale`), and two workers starting
the same run race on the one-open-run constraint."""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.agents.models import AgentProject
from apps.workspaces import services as wsvc

from . import reading, readers, runs
from .models import PROJECT, SYSTEM, BrainCapability, BrainCursor, BrainRun
from .registry import SCHEDULES

logger = logging.getLogger(__name__)


def projects_of(cap: BrainCapability):
    """The active workspace projects a project feature runs for: those of its operator's workspace and below."""
    tree = {cap.operator.workspace_id} | wsvc.descendant_slugs({cap.operator.workspace_id})
    return AgentProject.objects.filter(workspace_id__in=tree, visibility=AgentProject.WORKSPACE,
                                       status=AgentProject.ACTIVE).order_by("pk")


def due(cap: BrainCapability, project: AgentProject | None, now=None, scope: str = "") -> int | None:
    """The floor a run for this subject (a project, or None and the workspace `scope` for a system feature) starts
    from (always 0: `reading.candidates` leaves out what was learned), or None when none is due."""
    now = now or timezone.now()
    interval = SCHEDULES.get(cap.schedule)
    if not interval:
        return None
    subject_type, subject = runs.subject_of(cap, project, scope)
    if BrainRun.objects.filter(capability=cap, subject_type=subject_type, subject=subject,
                               state=BrainRun.OPEN).exists():
        return None
    cursor = BrainCursor.objects.filter(capability=cap, subject_type=subject_type, subject=subject).first()
    if cursor is not None and cursor.last_run_at is not None and (now - cursor.last_run_at).total_seconds() < interval:
        return None
    probe = BrainRun(capability=cap, subject_type=subject_type, subject=subject,
                     purpose=(cap.purposes or [""])[0], floor=0, scope=scope)
    return 0 if reading.candidates(probe).exists() else None


def subjects_of(cap: BrainCapability) -> list[tuple]:
    """What a scheduled feature runs for, as `(project, scope)`: each active workspace project in its own workspace,
    or for a system feature each workspace of its operator's tree (a workspace's run reads its subtree, so its owners
    and their ancestors' see what was learned there)."""
    if cap.purpose_class == PROJECT:
        return [(p, p.workspace_id) for p in projects_of(cap)]
    tree = {cap.operator.workspace_id} | wsvc.descendant_slugs({cap.operator.workspace_id})
    return [(None, slug) for slug in sorted(tree)]


def run_due(now=None) -> list[BrainRun]:
    """Start every run that is due. Safe from any number of workers: `runs.start` is one transaction and a
    second open run for the same project is refused by the database."""
    if not readers.enabled():
        return []
    runs.abandon_stale(now)
    started = []
    caps = (BrainCapability.objects.filter(enabled=True, purpose_class__in=[PROJECT, SYSTEM], operator__isnull=False)
            .exclude(schedule="").select_related("operator"))
    for cap in caps:
        for subject, scope in subjects_of(cap):
            try:
                floor = due(cap, subject, now, scope)
                if floor is None:
                    continue
                run = runs.start(cap, subject=subject, floor=floor, scope=scope)
                BrainCursor.objects.update_or_create(
                    capability=cap, subject_type=run.subject_type, subject=run.subject,
                    defaults={"last_run_at": now or timezone.now()})
                started.append(run)
            except runs.StartRefusedError:
                continue
            except Exception:  # noqa: BLE001 — one subject's trouble must not stop the others
                logger.exception("could not start %s for %s in %s", cap.slug, getattr(subject, "pk", "system"), scope)
    return started
