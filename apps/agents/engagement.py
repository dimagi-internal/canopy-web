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


# --- the report ------------------------------------------------------------------

#: What counts as a person REACTING to a proposed project (a view does not).
REACTIONS = ("backed", "commented", "approved", "declined")
_RUN_KINDS = ("run_announced", "run_confirmed", "run_skipped", "run_expired")


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def summary(workspaces, *, days: int = 14, group: str = "", agent: str = "",
            now: dt.datetime | None = None) -> dict:
    """What people actually did in `workspaces` over the last `days` — the report
    Ada reads weekly (`GET /api/engagement/summary`).

    * `counts` — events by (kind, surface);
    * `people` — the unique PEOPLE who did anything (agents' own logins excluded);
    * `proposals` — each project proposed in the window: time to the first human
      reaction (backed / commented / approved / declined), time to its decision,
      backers and comments; `unanswered` — those nobody reacted to at all;
    * `runs` — a work group's runs announced vs confirmed vs skipped vs expired,
      and the median time from announcement to confirmation. Run events pair by
      `meta.run` (the run id the layer-3 recorder puts there).

    `group` keeps one work group's events (and the projects proposed from it);
    `agent` keeps events by or about that agent's projects."""
    from django.db.models import Count, Q

    from .models import Agent, AgentProject

    now = now or timezone.now()
    since = now - dt.timedelta(days=max(1, min(days, 365)))
    qs = EngagementEvent.objects.filter(workspace_id__in=list(workspaces), at__gte=since)
    if group:
        qs = qs.filter(group=group)
    if agent:
        qs = qs.filter(Q(agent__slug=agent) | Q(project__agent__slug=agent))
    agent_users = set(Agent.objects.filter(user__isnull=False).values_list("user_id", flat=True))
    human = qs.filter(user__isnull=False).exclude(user_id__in=agent_users)

    counts = [{"kind": r["kind"], "surface": r["surface"], "n": r["n"]}
              for r in qs.values("kind", "surface").annotate(n=Count("pk")).order_by("kind", "surface")]
    people = [{"user_id": r["user_id"], "email": r["user__email"] or "", "events": r["n"]}
              for r in human.values("user_id", "user__email").annotate(n=Count("pk")).order_by("-n")]

    proposed_ids = set(qs.filter(kind="proposed", project__isnull=False).values_list("project_id", flat=True))
    proposals, unanswered = [], []
    projects = AgentProject.objects.filter(pk__in=proposed_ids).select_related("agent")
    for project in projects.order_by("proposed_at", "pk"):
        mine = EngagementEvent.objects.filter(project=project)
        born = (mine.filter(kind="proposed").order_by("at").values_list("at", flat=True).first()
                or project.proposed_at or project.created_at)
        reacted = (mine.filter(kind__in=REACTIONS, user__isnull=False).exclude(user_id__in=agent_users)
                   .order_by("at").values_list("at", flat=True).first())
        decided = (mine.filter(kind__in=("approved", "declined", "expired")).order_by("at")
                   .values_list("at", "kind").first())
        row = {"agent": project.agent.slug, "ref": project.ext_id, "name": project.name,
               "status": project.status, "proposed_at": born,
               "first_reaction_s": (reacted - born).total_seconds() if reacted else None,
               "decision": decided[1] if decided else None,
               "decision_s": (decided[0] - born).total_seconds() if decided else None,
               "backers": project.backings.count(),
               "comments": mine.filter(kind="commented").count()}
        proposals.append(row)
        if reacted is None:
            unanswered.append(row)

    runs = {k: 0 for k in _RUN_KINDS}
    announced: dict[str, dt.datetime] = {}
    waits: list[float] = []
    for kind, at, meta in qs.filter(kind__in=_RUN_KINDS).order_by("at").values_list("kind", "at", "meta"):
        runs[kind] += 1
        run = str((meta or {}).get("run") or "")
        if kind == "run_announced" and run:
            announced.setdefault(run, at)
        elif kind == "run_confirmed" and run in announced:
            waits.append((at - announced[run]).total_seconds())
    return {"since": since, "days": days, "counts": counts, "people": people,
            "proposals": proposals, "unanswered": unanswered,
            "runs": {"announced": runs["run_announced"], "confirmed": runs["run_confirmed"],
                     "skipped": runs["run_skipped"], "expired": runs["run_expired"],
                     "median_confirm_s": _median(waits)}}
