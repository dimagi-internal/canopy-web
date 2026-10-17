"""Recording what people do with projects and huddles (`EngagementEvent`).

`record` is the ONE writer, called from wherever an act happens: the backing
routes, the task actions on a project's tasks, idea creation, a work group's run
lifecycle, and the view beacon. Two promises to those callers:

* a bad `kind` or `surface` is a programming error and raises `ValueError` — a
  typo should fail its test, not write a row nobody counts;
* anything else that goes wrong (the database) is logged and swallowed. The act
  the caller is recording has already happened; failing it because the record of
  it could not be written would put telemetry on the main path.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.db import DatabaseError, transaction
from django.utils import timezone

from .models import EngagementEvent

log = logging.getLogger(__name__)

KINDS = frozenset(EngagementEvent.KINDS)
SURFACES = frozenset(EngagementEvent.SURFACES)


def record(kind: str, *, workspace, surface: str = "web", user=None, agent=None, project=None,
           task=None, huddle_id: str = "", group: str = "", meta: dict | None = None,
           dedupe_day: bool = False) -> EngagementEvent | None:
    """Append one event and return it — or None when `dedupe_day` found the same act
    already recorded today (UTC), or the write failed.

    `workspace` is a Workspace or its slug. `user` is None for machinery (an
    expiry) and for an agent acting as itself (pass `agent`). `group` is a work
    group's slug. `dedupe_day` keys on (kind, user, agent, project, huddle_id) —
    what the view beacon needs so a reload is not a second view."""
    if kind not in KINDS:
        raise ValueError(f"unknown engagement kind {kind!r}")
    if surface not in SURFACES:
        raise ValueError(f"unknown engagement surface {surface!r}")
    workspace_id = getattr(workspace, "pk", workspace)
    if not workspace_id:
        raise ValueError("an engagement event names its workspace")
    user = user if (user is not None and getattr(user, "is_authenticated", True)) else None
    fields = {"kind": kind, "user": user, "agent": agent, "project": project,
              "huddle_id": huddle_id or "", "group": group or ""}
    try:
        with transaction.atomic():
            if dedupe_day:
                start = timezone.now().astimezone(dt.UTC).replace(
                    hour=0, minute=0, second=0, microsecond=0)
                if EngagementEvent.objects.filter(workspace_id=workspace_id, at__gte=start,
                                                  **fields).exists():
                    return None
            return EngagementEvent.objects.create(workspace_id=workspace_id, surface=surface,
                                                  task=task, meta=meta or {}, **fields)
    except DatabaseError:
        log.exception("engagement: could not record %s in %s", kind, workspace_id)
        return None


def surface_of(request) -> str:
    """The surface a REST request came through: an MCP tool call dispatched in
    process carries `via_mcp` (apps/mcp/api_tools.py); everything else is web."""
    return "mcp" if getattr(request, "via_mcp", False) else "web"
