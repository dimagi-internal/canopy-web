"""/api/agents/{slug}/projects/… — proposing a project, and backing one.

Mounted beside the agents router. The rules live in `proposals.py`; this file is
the HTTP shape (each route is also an MCP tool):

* `POST …/projects/propose/` — create or revise a proposed project, its plan, its
  decision card and its partners' linked tasks, in one call. The agent itself, or
  an editor.
* `GET …/projects/{ref}/backings/` — where everyone stands. Members.
* `PUT / DELETE …/projects/{ref}/backings/me/` — the caller's own stance. Members;
  never an agent's login (an agent backs through the next route).
* `PUT …/projects/{ref}/backings/agents/{backer}/` — an agent's stance, called by
  that agent's own login or by the project's agent (`canopy huddle file` writes
  members' round-3 co-signs with the lead's token).
* `POST …/projects/{ref}/tasks/propose/` — a check-in's proposed TASK in the
  project (spec 2026-10-17-project-check-ins-design.md §3), and the same backing
  routes one level down: `…/projects/{ref}/tasks/{task}/backings/…`.

Starting or declining the project is NOT here: it is the decision card's approve
or decline (`POST …/tasks/{ref}/actions`), the existing task action.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth

from . import engagement, proposals, services
from .api import (
    _agent_for_write,
    _get_agent_or_404,
    _get_project_or_404,
    _is_agent_itself,
    _visible_agent_workspace_ids,
)
from .models import Agent, AgentTask
from .schemas import (
    AgentBackingIn,
    ProjectBackingIn,
    ProjectBackingOut,
    ProjectProposeIn,
    ProjectProposeOut,
    TaskProposeIn,
    TaskProposeOut,
)

router = Router(auth=session_auth, tags=["agents"])


def _partners(request: HttpRequest, slugs: list[str]) -> list[Agent]:
    """The partner agents, each one the caller can see — else 422 naming them."""
    visible = _visible_agent_workspace_ids(request)
    found, missing = [], []
    for slug in dict.fromkeys(s.strip() for s in slugs if s.strip()):
        agent = services.get_agent(slug)
        if agent is None or agent.workspace_id not in visible:
            missing.append(slug)
        else:
            found.append(agent)
    if missing:
        raise HttpError(422, f"unknown partner agent(s): {', '.join(missing)}")
    return found


def resolve_task_ref(request: HttpRequest, ref: str) -> AgentTask:
    """`<agent>/<task ext_id>` ("ada/T41") → that task, one the caller can see — else 422."""
    agent_slug, _, ext_id = (ref or "").strip().partition("/")
    agent = services.get_agent(agent_slug.strip()) if agent_slug.strip() and ext_id.strip() else None
    if agent is None or agent.workspace_id not in _visible_agent_workspace_ids(request):
        raise HttpError(422, f"no task {ref!r}: name it as <agent>/<task ext_id>, e.g. ada/T41")
    task = services.get_task(agent, ext_id.strip())
    if task is None:
        raise HttpError(422, f"{agent.slug} has no task {ext_id.strip()!r}")
    return task


@router.post("/{slug}/projects/propose/", response={200: ProjectProposeOut, 201: ProjectProposeOut},
             summary="Propose a project (or revise an open proposal of the same name)")
def propose_project(request: HttpRequest, slug: str, payload: ProjectProposeIn) -> Status:
    """Creates the project with `status=proposed`, its plan steps as `suggested`
    tasks, ONE decision card ("Start this project?", a review ask whose body is
    the why and the done-when) and one linked `suggested` task on each partner's
    own board. 201 when created; 200 when an open proposed project of this agent's
    with the same name was revised instead (its backings go stale, no second
    card). 409 when a project of that name was declined and no `new_evidence` is
    given. The agent itself, or an editor."""
    agent = _get_agent_or_404(request, slug)
    if not _is_agent_itself(request, agent):
        agent = _agent_for_write(request, slug)
    partners = _partners(request, payload.partners)
    from_task = resolve_task_ref(request, payload.from_task) if payload.from_task.strip() else None
    try:
        project, created = proposals.propose(
            agent, name=payload.name, outcome=payload.outcome, why=payload.why, plan=payload.plan,
            partners=partners, source_url=payload.source_url, owner_email=payload.owner_email,
            new_evidence=payload.new_evidence, from_task=from_task)
    except proposals.DeclinedBefore as exc:
        raise HttpError(409, str(exc)) from exc
    except services.UnknownPersonError as exc:
        raise HttpError(422, str(exc)) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    if created:
        engagement.record("proposed", workspace=agent.workspace_id, surface=engagement.surface_of(request),
                          agent=agent, project=project, task=proposals.decision_card(project),
                          checkin=payload.checkin)
    return Status(201 if created else 200,
                  {"created": created, "project": proposals.card(project, request.user)})


@router.get("/{slug}/projects/{ref}/backings/", response=list[ProjectBackingOut],
            summary="Where everyone stands on a project: backs, backs with changes, passes")
def list_project_backings(request: HttpRequest, slug: str, ref: str) -> list[dict]:
    """Oldest first. `stale` marks a backing of an earlier version (the project was
    revised since); `mine` marks the caller's own. Members."""
    from apps.contacts import services as contacts

    project = _get_project_or_404(_get_agent_or_404(request, slug), ref)
    return proposals.backings_of(project, me=contacts.person_for(user=request.user))


def _me(request: HttpRequest):
    from apps.contacts import services as contacts

    if Agent.objects.filter(user=request.user).exists():
        raise HttpError(422, "an agent backs a project as itself: PUT …/backings/agents/{agent}/")
    person = contacts.person_for(user=request.user)
    if person is None:
        raise HttpError(422, "canopy could not tell who you are")
    return person


@router.put("/{slug}/projects/{ref}/backings/me/", response=ProjectBackingOut,
            summary="Back a project, back it with changes, or pass — as yourself")
def back_project(request: HttpRequest, slug: str, ref: str, payload: ProjectBackingIn) -> dict:
    """Replaces your earlier stance. `line` (at most 280 characters) is required
    unless you simply back it. You become one of the project's participants. Any
    member; backing is a signal — it never starts the project."""
    project = _get_project_or_404(_get_agent_or_404(request, slug), ref)
    person = _me(request)
    try:
        proposals.back(project, stance=payload.stance, line=payload.line, person=person,
                       source=engagement.surface_of(request))
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    engagement.record("backed", workspace=project.agent.workspace_id,
                      surface=engagement.surface_of(request), user=request.user, project=project,
                      meta={"stance": payload.stance})
    return _one(project, person=person)


@router.delete("/{slug}/projects/{ref}/backings/me/", response={204: None},
               summary="Withdraw your backing of a project")
def unback_project(request: HttpRequest, slug: str, ref: str) -> Status:
    project = _get_project_or_404(_get_agent_or_404(request, slug), ref)
    proposals.unback(project, _me(request))
    return Status(204, None)


@router.put("/{slug}/projects/{ref}/backings/agents/{backer}/", response=ProjectBackingOut,
            summary="Record an agent's stance on a project (a co-sign)")
def back_project_as_agent(request: HttpRequest, slug: str, ref: str, backer: str,
                          payload: AgentBackingIn) -> dict:
    """Called by `backer`'s own login, or by the project's agent's (a huddle's lead
    files its members' round-3 answers: co-sign → backs, amend →
    backs_with_changes, decline → passes). Replaces that agent's earlier stance."""
    agent = _get_agent_or_404(request, slug)
    project = _get_project_or_404(agent, ref)
    who = services.get_agent(backer)
    if who is None or who.workspace_id not in _visible_agent_workspace_ids(request):
        raise HttpError(404, f"agent '{backer}' not found")
    if not (_is_agent_itself(request, who) or _is_agent_itself(request, agent)):
        raise HttpError(403, f"only {backer} itself, or {agent.slug} (the project's agent), "
                             f"records {backer}'s stance")
    try:
        proposals.back(project, stance=payload.stance, line=payload.line, agent=who,
                       source="huddle" if payload.huddle_id else engagement.surface_of(request),
                       huddle_id=payload.huddle_id)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    engagement.record("backed", workspace=agent.workspace_id, surface=engagement.surface_of(request),
                      agent=who, project=project, huddle_id=payload.huddle_id,
                      meta={"stance": payload.stance})
    return _one(project, agent=who)


def _one(project, *, person=None, agent=None) -> dict:
    [row] = [b for b in proposals.backings_of(project, me=person)
             if (b["backer"]["person_id"] == getattr(person, "pk", None) if person is not None
                 else b["backer"]["agent"] == agent.slug)]
    return row


# ---- proposed tasks: what a project's check-in produces ---------------------------


def _task_in(project, ref: str) -> AgentTask:
    task = services.get_task(project.agent, ref)
    if task is None or task.project_id != project.pk:
        raise HttpError(404, f"task {ref} not found in {project.ext_id}")
    return task


@router.post("/{slug}/projects/{ref}/tasks/propose/", response={200: TaskProposeOut, 201: TaskProposeOut},
             summary="Propose a task in a project (or revise an open proposed task of the same title)")
def propose_task(request: HttpRequest, slug: str, ref: str, payload: TaskProposeIn) -> Status:
    """The task-level twin of `projects/propose/`, and what a project's check-in
    files: one task, `status=suggested`, in the project, its `plan` steps as
    child tasks, and one linked `suggested` task on each partner's own board.
    `run` names the check-in run it came from. 201 when created; 200 when an open
    proposed task of the same title was revised instead (its backings go stale).
    409 when that title was declined before and no `new_evidence` is given.
    Start and Decline are the task's own approve and decline (`POST
    …/tasks/{task}/actions`), by the project's owner, the check-in's lead, or a
    workspace owner or admin. The project's agent itself, or an editor."""
    from apps.huddles.models import CheckinRun

    agent = _get_agent_or_404(request, slug)
    if not _is_agent_itself(request, agent):
        agent = _agent_for_write(request, slug)
    project = _get_project_or_404(agent, ref)
    partners = _partners(request, payload.partners)
    run = None
    if payload.run is not None:
        run = CheckinRun.objects.filter(pk=payload.run, project=project).first()
        if run is None:
            raise HttpError(422, f"check-in run {payload.run} is not {project.ext_id}'s")
    try:
        task, created = proposals.propose_task(
            project, title=payload.title, why=payload.why, done_when=payload.done_when, plan=payload.plan,
            partners=partners, source_url=payload.source_url, run=run, new_evidence=payload.new_evidence)
    except proposals.DeclinedBefore as exc:
        raise HttpError(409, str(exc)) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    if created:
        engagement.record("proposed", workspace=agent.workspace_id, surface=engagement.surface_of(request),
                          agent=agent, project=project, task=task, huddle_id=task.origin_ref.get("huddle_id", ""),
                          checkin=str(run.checkin_id or "") if run is not None else "",
                          meta={"level": "task", **({"run": run.pk} if run is not None else {})})
    return Status(201 if created else 200, {"created": created, "task": proposals.task_card(task, request.user)})


@router.get("/{slug}/projects/{ref}/tasks/{task}/backings/", response=list[ProjectBackingOut],
            summary="Where everyone stands on a proposed task")
def list_task_backings(request: HttpRequest, slug: str, ref: str, task: str) -> list[dict]:
    """Oldest first. `stale` marks a backing of an earlier version; `mine` the caller's own. Members."""
    from apps.contacts import services as contacts

    row = _task_in(_get_project_or_404(_get_agent_or_404(request, slug), ref), task)
    return proposals.backings_of(row, me=contacts.person_for(user=request.user))


@router.put("/{slug}/projects/{ref}/tasks/{task}/backings/me/", response=ProjectBackingOut,
            summary="Back a proposed task, back it with changes, or pass — as yourself")
def back_task(request: HttpRequest, slug: str, ref: str, task: str, payload: ProjectBackingIn) -> dict:
    """Replaces your earlier stance. `line` (at most 280 characters) is required
    unless you simply back it. You become one of the project's participants. Any
    member; backing is a signal — it never starts the task."""
    project = _get_project_or_404(_get_agent_or_404(request, slug), ref)
    row = _task_in(project, task)
    person = _me(request)
    try:
        proposals.back(None, task=row, stance=payload.stance, line=payload.line, person=person,
                       source=engagement.surface_of(request))
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    engagement.record("backed", workspace=project.agent.workspace_id,
                      surface=engagement.surface_of(request), user=request.user, project=project, task=row,
                      meta={"stance": payload.stance})
    return _one(row, person=person)


@router.delete("/{slug}/projects/{ref}/tasks/{task}/backings/me/", response={204: None},
               summary="Withdraw your backing of a proposed task")
def unback_task(request: HttpRequest, slug: str, ref: str, task: str) -> Status:
    project = _get_project_or_404(_get_agent_or_404(request, slug), ref)
    proposals.unback(None, _me(request), task=_task_in(project, task))
    return Status(204, None)


@router.put("/{slug}/projects/{ref}/tasks/{task}/backings/agents/{backer}/", response=ProjectBackingOut,
            summary="Record an agent's stance on a proposed task (a co-sign)")
def back_task_as_agent(request: HttpRequest, slug: str, ref: str, task: str, backer: str,
                       payload: AgentBackingIn) -> dict:
    """Called by `backer`'s own login, or by the project's agent's (`canopy huddle
    file` records members' round-3 co-signs: co-sign → backs, amend →
    backs_with_changes, decline → passes). Replaces that agent's earlier stance."""
    agent = _get_agent_or_404(request, slug)
    project = _get_project_or_404(agent, ref)
    row = _task_in(project, task)
    who = services.get_agent(backer)
    if who is None or who.workspace_id not in _visible_agent_workspace_ids(request):
        raise HttpError(404, f"agent '{backer}' not found")
    if not (_is_agent_itself(request, who) or _is_agent_itself(request, agent)):
        raise HttpError(403, f"only {backer} itself, or {agent.slug} (the project's agent), "
                             f"records {backer}'s stance")
    try:
        proposals.back(None, task=row, stance=payload.stance, line=payload.line, agent=who,
                       source="huddle" if payload.huddle_id else engagement.surface_of(request),
                       huddle_id=payload.huddle_id)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    engagement.record("backed", workspace=agent.workspace_id, surface=engagement.surface_of(request),
                      agent=who, project=project, task=row, huddle_id=payload.huddle_id,
                      meta={"stance": payload.stance})
    return _one(row, agent=who)
