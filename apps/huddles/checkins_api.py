"""Project check-ins and their runs (apps/huddles/checkins.py) over REST — each
route is also an MCP tool.

* `GET/POST /api/agents/{slug}/projects/{ref}/checkins/`,
  `GET/PATCH/DELETE …/checkins/{name}`, `POST …/checkins/{name}/run-now`
* `GET /api/agents/{slug}/projects/{ref}/checkin-runs` — the project's timeline
* `GET /api/checkins/` — every check-in in your workspaces (a navigation view)
* `GET /api/checkins/runs/{id}` (with its material), `POST …/runs/{id}/finished`
* `GET /api/huddle-groups/runs/{id}` and `…/finished` — aliases for one release

Members read; the project's owner or a workspace owner or admin writes. Nothing
here reads a Slack channel or can start a read: a run is read only when its lead
presses Confirm on the announcement in Slack. Run now announces (or, with no
channel, queues the turn) — it never reads. `finished` is the project agent's
own login reporting the run done.
"""
from __future__ import annotations

from canopy_cron import next_slots
from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status
from ninja.errors import HttpError

from apps.agents.models import AgentProject, AgentTask
from apps.api.auth import session_auth
from apps.workspaces import services as wsvc

from . import checkins
from .models import CheckinRun, ProjectCheckin
from .schemas import (
    CheckinIn,
    CheckinOut,
    CheckinPatch,
    CheckinRunFinishIn,
    CheckinRunOut,
    CheckinTimelineRunOut,
    LegacyGroupRunOut,
)

#: Mounted at /api/agents: a project's check-ins and its timeline.
project_router = Router(auth=session_auth, tags=["huddles"])
#: Mounted at /api/checkins.
router = Router(tags=["huddles"])
#: Mounted at /api/huddle-groups: the two run routes an un-updated engine still calls.
legacy_router = Router(tags=["huddles"])


def _project_ref(p: AgentProject) -> dict:
    return {"agent": p.agent.slug, "ref": p.ext_id, "name": p.name, "status": p.status,
            "url": checkins.project_path(p), "workspace": p.agent.workspace_id}


def checkin_out(c: ProjectCheckin, user) -> dict:
    s = c.schedule
    try:
        nxt = next_slots(s.cron, s.timezone, now=timezone.now(), count=3) if s is not None and c.enabled else []
    except Exception:  # noqa: BLE001
        nxt = []
    p = c.project
    last = c.runs.order_by("-created_at").values_list("status", flat=True).first()
    return {
        "id": c.pk, "name": c.name, "project": _project_ref(p), "workspace": p.agent.workspace_id,
        "topic": c.topic, "lead_email": getattr(c.lead, "email", "") or "", "lead_slack_id": c.lead_slack_id,
        "channel_id": c.channel_id, "channel_name": c.channel_name, "agents": list(c.agents or []),
        "agent": p.agent.slug, "format": c.format,
        "cron": s.cron if s else "", "timezone": s.timezone if s else "UTC", "next_runs": nxt,
        "schedule_id": s.pk if s else None, "enabled": c.enabled, "added_allowance": c.added_allowance,
        "last_run_status": last or "", "legacy_slug": c.legacy_slug, "url": checkins.project_path(p),
        "can_edit": checkins.may_write(user, p), "created_at": c.created_at, "updated_at": c.updated_at,
    }


def run_out(r: CheckinRun, *, material: bool = False) -> dict:
    p = r.project
    parts = r.participants if isinstance(r.participants, dict) else {}
    return {
        "id": r.pk, "checkin": r.checkin_id, "checkin_name": r.checkin_name, "project": _project_ref(p),
        "workspace": p.agent.workspace_id, "slot": r.slot, "manual": r.manual, "status": r.status,
        "note": r.note,
        "participants": {"agents": list(parts.get("agents") or []), "lead_email": parts.get("lead_email") or ""},
        "channel_id": r.channel_id, "channel_name": r.channel_name, "announce_ts": r.announce_ts,
        "slack_url": (f"https://slack.com/archives/{r.channel_id}/p{r.announce_ts.replace('.', '')}"
                      if r.announce_ts and r.channel_id else ""),
        "confirmed_by_email": getattr(r.confirmed_by, "email", "") or "", "confirmed_at": r.confirmed_at,
        "read_messages": r.read_messages, "read_first_ts": r.read_first_ts, "read_last_ts": r.read_last_ts,
        "read_since": r.read_since, "material": r.material if material else "",
        "turn_id": str(r.turn_id or ""), "huddle_id": r.huddle_id,
        "huddle_url": f"/w/{p.agent.workspace_id}/huddles/{r.huddle_id}" if r.huddle_id else "",
        "result_url": r.result_url, "summary": r.summary, "tasks_proposed": r.tasks_proposed,
        "finished_at": r.finished_at, "created_at": r.created_at,
    }


def _qs():
    return ProjectCheckin.objects.select_related("project", "project__agent", "project__owner_user", "lead",
                                                 "schedule", "installation")


def _project(request: HttpRequest, slug: str, ref: str) -> AgentProject:
    from apps.agents.gates import agent_for_read, project_or_404

    return project_or_404(agent_for_read(request, slug), ref)


def _checkin_or_404(project: AgentProject, name: str) -> ProjectCheckin:
    c = _qs().filter(project=project, name__iexact=name.strip()).first()
    if c is None and name.strip().isdigit():
        c = _qs().filter(project=project, pk=int(name.strip())).first()
    if c is None:
        raise HttpError(404, f"{project.name} has no check-in {name!r}")
    return c


def _require_writer(request: HttpRequest, project: AgentProject) -> None:
    if not checkins.may_write(request.user, project):
        raise HttpError(403, f"only the project's owner, or an owner or admin of {project.agent.workspace_id}, "
                             "can change its check-ins")


def _save(request: HttpRequest, project: AgentProject, data: dict, checkin: ProjectCheckin | None = None):
    try:
        return checkins.save(project=project, by=request.user, data=data, checkin=checkin)
    except checkins.CheckinError as e:
        raise HttpError(e.status, str(e)) from e


# ---- a project's check-ins ------------------------------------------------------


@project_router.get("/{slug}/projects/{ref}/checkins/", response=list[CheckinOut],
                    summary="List a project's check-ins")
def list_project_checkins(request: HttpRequest, slug: str, ref: str) -> list[dict]:
    """The project's recurring meetings: cadence, lead, agents, channel (if any). Members."""
    project = _project(request, slug, ref)
    return [checkin_out(c, request.user) for c in _qs().filter(project=project).order_by("name")]


@project_router.post("/{slug}/projects/{ref}/checkins/", response={201: CheckinOut},
                     summary="Add a check-in to a project")
def create_project_checkin(request: HttpRequest, slug: str, ref: str, payload: CheckinIn):
    """The project's owner, or a workspace owner or admin. Checks the lead is in
    the workspace (and, with a channel, in the channel) and that the project's
    agent may run every other agent; with a channel, puts it on the agent's read
    allow-list (reusing an entry already there — only an owner or admin adds a
    new one); creates the schedule (`/canopy:huddle`, the check-in's cadence) on
    the project's agent. Reads nothing: each run is read only when the lead
    presses Confirm on its announcement in Slack. Without a channel, each run
    queues the agent's turn directly and reads nothing."""
    project = _project(request, slug, ref)
    _require_writer(request, project)
    c = _save(request, project, payload.model_dump())
    return Status(201, checkin_out(_qs().get(pk=c.pk), request.user))


@project_router.get("/{slug}/projects/{ref}/checkins/{name}", response=CheckinOut,
                    summary="Get one of a project's check-ins")
def get_project_checkin(request: HttpRequest, slug: str, ref: str, name: str) -> dict:
    return checkin_out(_checkin_or_404(_project(request, slug, ref), name), request.user)


@project_router.patch("/{slug}/projects/{ref}/checkins/{name}", response=CheckinOut,
                      summary="Change a project's check-in")
def update_project_checkin(request: HttpRequest, slug: str, ref: str, name: str, payload: CheckinPatch) -> dict:
    """The project's owner, or a workspace owner or admin. Send only what changes
    (`channel: ""` takes the channel off); every check runs again."""
    project = _project(request, slug, ref)
    c = _checkin_or_404(project, name)
    _require_writer(request, project)
    c = _save(request, project, payload.model_dump(exclude_none=True), checkin=c)
    return checkin_out(_qs().get(pk=c.pk), request.user)


@project_router.delete("/{slug}/projects/{ref}/checkins/{name}", response={204: None},
                       summary="Delete a project's check-in")
def delete_project_checkin(request: HttpRequest, slug: str, ref: str, name: str):
    """The project's owner, or a workspace owner or admin. Takes the channel off
    the agent's read allow-list only if this check-in put it there (and no other
    check-in still uses it), deletes the schedule, and lapses any announcement
    still waiting for a confirm. Its runs stay on the project's timeline."""
    project = _project(request, slug, ref)
    c = _checkin_or_404(project, name)
    _require_writer(request, project)
    checkins.delete(c, by=request.user)
    return Status(204, None)


@project_router.post("/{slug}/projects/{ref}/checkins/{name}/run-now", response={202: CheckinRunOut},
                     summary="Run a project's check-in now (announces; never reads)")
def run_project_checkin_now(request: HttpRequest, slug: str, ref: str, name: str):
    """The project's owner, or a workspace owner or admin. With a channel: posts
    the announcement now — the channel is read only if the lead presses Confirm
    in Slack. Without one: queues the agent's turn now (nothing to read)."""
    project = _project(request, slug, ref)
    c = _checkin_or_404(project, name)
    _require_writer(request, project)
    if c.schedule is None:
        raise HttpError(409, f"{c.name} has no schedule — save it again")
    run, _ = checkins.announce(c.schedule, timezone.now(), manual=True)
    return Status(202, run_out(_run_qs().get(pk=run.pk)))


@project_router.get("/{slug}/projects/{ref}/checkin-runs", response=list[CheckinTimelineRunOut],
                    summary="A project's check-in timeline")
def list_project_checkin_runs(request: HttpRequest, slug: str, ref: str, limit: int = 50) -> list[dict]:
    """Every check-in run on the project, newest first: who took part, the read
    window, who confirmed it, the huddle, and what it proposed — tasks with their
    live status and any projects they spawned (and, for a huddle from before
    check-ins proposed tasks, the projects it proposed). No material."""
    from apps.agents import proposals

    project = _project(request, slug, ref)
    runs = list(_run_qs().filter(project=project).order_by("-created_at")[:max(1, min(limit, 200))])
    by_run: dict[int, list[AgentTask]] = {}
    for t in proposals.proposed_tasks_of(project).prefetch_related("spawned_projects__agent"):
        for rid in proposals.task_runs(t):
            by_run.setdefault(rid, []).append(t)
    out = []
    for r in runs:
        row = run_out(r)
        row["tasks"] = [{"agent": t.agent.slug, "ref": t.ext_id, "title": t.title, "status": t.status,
                         "url": checkins.project_path(project), "spawned": proposals.spawned_of(t)}
                        for t in sorted(by_run.get(r.pk, []), key=lambda t: t.pk)]
        row["projects"] = _huddle_projects(r)
        out.append(row)
    return out


def _huddle_projects(r: CheckinRun) -> list[dict]:
    if not r.huddle_id:
        return []
    tail = Q(source_url__endswith=f"/huddles/{r.huddle_id}") | Q(source_url__endswith=f"/huddles/{r.huddle_id}/")
    tree = checkins._tree(r.project.agent.workspace_id)
    rows = (AgentProject.objects.select_related("agent").filter(tail, agent__workspace_id__in=tree)
            .order_by("agent__slug", "pk"))
    return [{"agent": p.agent.slug, "ref": p.ext_id, "name": p.name, "status": p.status,
             "url": checkins.project_path(p)} for p in rows]


# ---- across projects --------------------------------------------------------------


@router.get("/", response=list[CheckinOut], summary="List check-ins across your projects")
def list_checkins(request: HttpRequest) -> list[dict]:
    """Every check-in on a project in your workspaces — a navigation view; each
    one's record lives on its project's page."""
    rows = (_qs().filter(project__agent__workspace_id__in=wsvc.request_workspace_slugs(request))
            .order_by("project__name", "name"))
    return [checkin_out(c, request.user) for c in rows]


def _run_qs():
    return CheckinRun.objects.select_related("checkin", "project", "project__agent", "confirmed_by")


def _run_or_404(request: HttpRequest, run_id: int) -> CheckinRun:
    r = (_run_qs().filter(pk=run_id)
         .filter(Q(project__agent__workspace_id__in=wsvc.request_workspace_slugs(request))
                 | Q(project__agent__user_id=request.user.pk)).first())
    if r is None:
        raise HttpError(404, f"check-in run {run_id} not found")
    return r


def _finish(request: HttpRequest, run_id: int, payload: CheckinRunFinishIn) -> CheckinRun:
    r = _run_or_404(request, run_id)
    agent = r.project.agent
    if agent.user_id is None or agent.user_id != request.user.pk:
        raise HttpError(403, f"only {agent.slug}'s own login can finish this run")
    try:
        r = checkins.finish(r, huddle_id=payload.huddle_id, result_url=payload.result_url,
                            summary=payload.summary, tasks_proposed=payload.tasks_proposed)
    except checkins.CheckinError as e:
        raise HttpError(e.status, str(e)) from e
    return _run_qs().get(pk=r.pk)


@router.get("/runs/{run_id}", response=CheckinRunOut, summary="Get one check-in run, with its material")
def get_checkin_run(request: HttpRequest, run_id: int) -> dict:
    """One run: status, who took part, the read window and the channel material
    it read (members of the project's workspace, and the project agent's own login)."""
    return run_out(_run_or_404(request, run_id), material=True)


@router.post("/runs/{run_id}/finished", response=CheckinRunOut, summary="Report a check-in run done")
def finish_checkin_run(request: HttpRequest, run_id: int, payload: CheckinRunFinishIn) -> dict:
    """The project agent's own login only. Marks the run done, records how many
    tasks it proposed, and (with a channel) posts one line in the announcement's
    thread pointing at the result — the content stays in canopy."""
    return run_out(_finish(request, run_id, payload), material=True)


# ---- the old work-group run routes, for one release ---------------------------------


def _legacy(row: dict) -> dict:
    return {**row, "group": row["checkin_name"], "group_name": row["checkin_name"]}


@legacy_router.get("/runs/{run_id}", response=LegacyGroupRunOut,
                   summary="Deprecated: GET /api/checkins/runs/{id}")
def get_huddle_group_run(request: HttpRequest, run_id: int) -> dict:
    """Kept for one release so an engine that still says `--group-run` reads its
    run. Use `GET /api/checkins/runs/{id}`."""
    return _legacy(run_out(_run_or_404(request, run_id), material=True))


@legacy_router.post("/runs/{run_id}/finished", response=LegacyGroupRunOut,
                    summary="Deprecated: POST /api/checkins/runs/{id}/finished")
def finish_huddle_group_run(request: HttpRequest, run_id: int, payload: CheckinRunFinishIn) -> dict:
    """Kept for one release so an engine that still says `--group-run` finishes
    its run. Use `POST /api/checkins/runs/{id}/finished`."""
    return _legacy(run_out(_finish(request, run_id, payload), material=True))
