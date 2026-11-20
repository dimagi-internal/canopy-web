"""Starting runs on a schedule: one per active project per feature, only when there is something new.

For each enabled feature that names a `schedule`, an operator and a project class, and each active workspace
project in the operator's workspace tree, `due` asks: has a run started in the last interval, is one open, and are
there eligible sources past the feature's cursor for that project? Only then does `run_due` start one, reading past
the cursor (`BrainRun.floor`), so a project is never re-read from the start. A judge (`reading.JUDGE`) has no cursor:
what it has ruled on leaves its candidates. A failed run moves nothing and is tried again at the next interval."""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.agents.models import AgentProject
from apps.workspaces import services as wsvc

from . import reading, readers, runs
from .models import PROJECT, BrainCapability, BrainCursor, BrainRun
from .registry import SCHEDULES

logger = logging.getLogger(__name__)


def projects_of(cap: BrainCapability):
    """The active workspace projects a project feature runs for: those of its operator's workspace and below."""
    tree = {cap.operator.workspace_id} | wsvc.descendant_slugs({cap.operator.workspace_id})
    return AgentProject.objects.filter(workspace_id__in=tree, visibility=AgentProject.WORKSPACE,
                                       status=AgentProject.ACTIVE).order_by("pk")


def due(cap: BrainCapability, project: AgentProject, now=None) -> int | None:
    """The floor a run for this project should read past, or None when none is due."""
    now = now or timezone.now()
    interval = SCHEDULES.get(cap.schedule)
    if not interval:
        return None
    if BrainRun.objects.filter(capability=cap, subject_type="project", subject=str(project.pk),
                               state=BrainRun.OPEN).exists():
        return None
    cursor = BrainCursor.objects.filter(capability=cap, subject_type="project", subject=str(project.pk)).first()
    if cursor is not None and cursor.last_run_at is not None and (now - cursor.last_run_at).total_seconds() < interval:
        return None
    purpose = (cap.purposes or [""])[0]
    probe = BrainRun(capability=cap, subject_type="project", subject=str(project.pk), purpose=purpose,
                     floor=0 if purpose == reading.JUDGE else (cursor.last_source if cursor else 0))
    return probe.floor if reading.candidates(probe).exists() else None


def run_due(now=None) -> list[BrainRun]:
    """Start every run that is due. Safe from any number of workers: `runs.start` is one transaction and a
    project with an open run is skipped."""
    if not readers.enabled():
        return []
    started = []
    caps = (BrainCapability.objects.filter(enabled=True, purpose_class=PROJECT, operator__isnull=False)
            .exclude(schedule="").select_related("operator"))
    for cap in caps:
        for project in projects_of(cap):
            try:
                floor = due(cap, project, now)
                if floor is None:
                    continue
                run = runs.start(cap, subject=project, floor=floor)
                BrainCursor.objects.update_or_create(
                    capability=cap, subject_type="project", subject=str(project.pk),
                    defaults={"last_run_at": now or timezone.now()})
                started.append(run)
            except runs.StartRefusedError:
                continue
            except Exception:  # noqa: BLE001 — one project's trouble must not stop the others
                logger.exception("could not start %s for project %s", cap.slug, project.pk)
    return started
