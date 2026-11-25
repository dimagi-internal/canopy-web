"""Who manages whom (`ReportingLine`): set by a workspace owner by hand; every change is an event.

It opens one thing: a manager reading their reports' `individual` OUTPUTS (`outputs.visible_to`).
Never a transcript, never any other class's output. Canopy had no manager relationship before this."""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.events import services as events
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from .models import ReportingLine

logger = logging.getLogger(__name__)

SOURCE = "brain.reporting"
SET, ENDED = "reporting.set", "reporting.ended"


class RefusedError(ValueError):
    """The line asked for cannot be made (a 422 at the route)."""


def _record(kind: str, line: ReportingLine, by) -> None:
    try:
        events.record([{"source": SOURCE, "kind": kind,
                        "payload": {"person_id": line.person_id, "manager_id": line.manager_id,
                                    "by_id": getattr(by, "pk", None)}}],
                      workspace=Workspace(pk=line.workspace_id))
    except Exception:  # noqa: BLE001 — a record is never worth a failed change
        logger.exception("could not record %s in %s", kind, line.workspace_id)


def live(workspace) -> list[ReportingLine]:
    return list(ReportingLine.objects.filter(workspace=workspace, ended_at__isnull=True)
                .select_related("person", "manager").order_by("person__email", "pk"))


@transaction.atomic
def set_manager(workspace, *, person, manager, by) -> ReportingLine:
    """Make `manager` `person`'s manager in `workspace`, replacing any earlier one. Both must be members
    (a guest is not), and nobody manages themselves. The caller checked `by` owns the workspace."""
    if person.pk == manager.pk:
        raise RefusedError("nobody is their own manager")
    for who in (person, manager):
        if not wsvc.is_member(who, workspace.slug):
            raise RefusedError(f"{who.email or who.username} is not a member of this workspace")
    current = ReportingLine.objects.filter(workspace=workspace, person=person, ended_at__isnull=True).first()
    if current is not None and current.manager_id == manager.pk:
        return current
    if current is not None:
        current.ended_at = timezone.now()
        current.save(update_fields=["ended_at"])
        _record(ENDED, current, by)
    line = ReportingLine.objects.create(workspace=workspace, person=person, manager=manager, set_by=by)
    _record(SET, line, by)
    return line


@transaction.atomic
def end(workspace, *, person, by) -> bool:
    line = ReportingLine.objects.filter(workspace=workspace, person=person, ended_at__isnull=True).first()
    if line is None:
        return False
    line.ended_at = timezone.now()
    line.save(update_fields=["ended_at"])
    _record(ENDED, line, by)
    return True


def reports_of(manager, workspace_slugs=None):
    """The ids of the people `manager` manages now, in the given workspaces (all of theirs by default)."""
    qs = ReportingLine.objects.filter(manager=manager, ended_at__isnull=True)
    if workspace_slugs is not None:
        qs = qs.filter(workspace_id__in=list(workspace_slugs))
    return set(qs.values_list("person_id", flat=True))


def lines_of(manager, workspace_slugs=None) -> list[tuple[str, int]]:
    """`(workspace, person id)` for each live line `manager` manages: a line opens its own workspace only."""
    qs = ReportingLine.objects.filter(manager=manager, ended_at__isnull=True)
    if workspace_slugs is not None:
        qs = qs.filter(workspace_id__in=list(workspace_slugs))
    return list(qs.values_list("workspace_id", "person_id"))
