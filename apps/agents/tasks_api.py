"""The workspace's work, by its own address: one task by key
(`/api/tasks/{key}/`), bulk edits, relations, Triage, labels, saved views, a
workspace's task settings, and a project's milestones and updates.

The lists are `fleet_api` (`GET /api/tasks/`, `/api/projects/`); an agent's
view of the same rows is `api_board`. Design:
`docs/superpowers/specs/2026-10-24-workspace-tasks-and-projects-design.md`.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.pagination import DEFAULT_LIMIT_CAP, clamp_limit
from apps.api.route_gates import gate
from apps.events import audit
from apps.harness import turn_status
from apps.harness.models import Turn
from apps.workspaces.links import base_url
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import proposals, services, work
from .api_board import act_and_record, apply_task_patch
from .gates import (
    listed_agent_workspaces,
    may_edit_project,
    may_edit_task,
    project_ws_or_404,
    require_project_edit,
    task_or_404,
    visible_agent_workspaces,
)
from .models import AgentProject, AgentTask, Label, ProjectBacking, ProjectUpdate, SavedView
from .schemas import (
    ActOut,
    AgentTaskActionIn,
    AgentTaskActionOut,
    AgentTaskOut,
    AgentTaskPatch,
    BulkTaskIn,
    BulkTaskResultOut,
    LabelIn,
    LabelOut,
    LabelPatch,
    MilestoneIn,
    MilestoneOut,
    MilestonePatch,
    ProjectServiceIn,
    ProjectServiceOut,
    ProjectServicePatch,
    ProjectUpdateIn,
    ProjectUpdateOut,
    ProjectUpdatePublishIn,
    SavedViewIn,
    SavedViewOut,
    SavedViewPatch,
    TaskBriefOut,
    TaskDetailOut,
    TaskEventOut,
    TaskRelationIn,
    TaskRelationOut,
    TaskSettingsOut,
    TaskSettingsPatch,
    TaskTurnOut,
    TriageOut,
)

router = Router(auth=session_auth, tags=["tasks"])


def _brief(task: AgentTask) -> TaskBriefOut:
    return TaskBriefOut(key=task.key, title=task.title, status=task.status, category=task.category,
                        url=task.url)


def _event_out(e) -> TaskEventOut:
    user = e.by_user
    return TaskEventOut(
        id=e.pk, kind=e.kind, field=e.field, old=e.old, new=e.new,
        comment=e.action.comment if e.action_id else "",
        by_email=user.email if user else (e.action.by if e.action_id and "@" in e.action.by else None),
        by_name=(user.get_full_name() or user.get_username()) if user else "",
        by_agent=e.by_agent.slug if e.by_agent_id else None, created_at=e.created_at)


def _turn_of(task: AgentTask) -> TaskTurnOut | None:
    """The newest turn this task raised, as `turn_status` says it."""
    turn = (Turn.objects.filter(raised_from_task=task).select_related("agent", "claimed_by")
            .order_by("-created_at").first())
    if turn is None:
        return None
    try:
        state = turn_status.resolve(turn).state
    except Exception:  # noqa: BLE001 — a task page never fails over its turn's state
        state = turn.status
    return TaskTurnOut(id=turn.id, status=turn.status, state=state, created_at=turn.created_at,
                       url=f"/w/{turn.agent.workspace_id}/agents/{turn.agent.slug}/turns" if turn.agent_id else "")


def task_detail(request: HttpRequest, task: AgentTask) -> TaskDetailOut:
    task = work.task_rows(AgentTask.objects.filter(pk=task.pk)).get()
    subtasks = list(work.task_rows(task.subtasks.all()).order_by("rank", "id"))
    return TaskDetailOut(
        **AgentTaskOut.model_validate(task).model_dump(),
        actions=[AgentTaskActionOut.model_validate(a) for a in task.actions.select_related("agent", "task")],
        events=[_event_out(e) for e in work.events_of(task)],
        relations=[TaskRelationOut(kind=r["kind"], task=_brief(r["task"])) for r in work.relations_of(task)],
        subtasks=[AgentTaskOut.model_validate(t) for t in subtasks],
        parent=_brief(task.parent) if task.parent_id else None,
        turn=_turn_of(task),
        blocked=work.is_blocked(task),
        may_edit=may_edit_task(request, task),
        status_names=work.status_names(task.workspace_id),
    )


# ---- one task ----------------------------------------------------------------


@router.get("/tasks/{key}/", response=TaskDetailOut,
            summary="Get one task by key: its fields, history, relations, subtasks and turn")
@gate("member")
def get_task_by_key(request: HttpRequest, key: str) -> TaskDetailOut:
    return task_detail(request, task_or_404(request, key))


@router.patch("/tasks/{key}/", response=AgentTaskOut, summary="Update a task's fields, by key")
# an editor of its workspace, its assignee person, or its agent's own login
@gate("agent.work", "agent-login", "self", kind="content")
def patch_task_by_key(request: HttpRequest, key: str, payload: AgentTaskPatch) -> AgentTaskOut:
    """Each field that changes is a history event. `assignee`: an email, an
    agent's slug or `""`; `labels` replaces the list; `before`/`after` (keys)
    place it between two tasks."""
    task = task_or_404(request, key)
    if not may_edit_task(request, task):
        raise HttpError(403, "changing a task requires the editor role, being its assignee, or being its agent")
    return AgentTaskOut.model_validate(apply_task_patch(request, task, payload.model_dump(exclude_unset=True)))


@router.post("/tasks/{key}/actions", response=ActOut,
             summary="Act on a task by key: approve, decline, reply, accept, backlog, snooze, duplicate, nudge, done")
@gate(
    "member",
    "agent.work",
    kind="content",
    viewer_write=(
        "approve/decline/reply/accept/backlog/snooze/duplicate decide what is already on the board (the "
        "interaction tier); nudge/done require agent.work — except that a task's own assignee, or the person "
        "whose idea it is, may mark it done"
    ),
)
def act_on_task_by_key(request: HttpRequest, key: str, payload: AgentTaskActionIn) -> ActOut:
    """approve → in progress and, on an agent's task, its agent starts now.
    accept → todo; backlog → backlog (both close a review ask, start nothing).
    snooze (comment: a date, default a week) → out of Triage until then.
    duplicate (comment: the key it duplicates) → canceled, related. decline,
    reply, nudge and done as on an agent's route."""
    return act_and_record(request, task_or_404(request, key), payload)


@router.post("/tasks/bulk", response=list[BulkTaskResultOut], summary="Change many tasks at once")
# each task is gated as its own PATCH / action: the editor tier, or member for the deciding actions
@gate(
    "member",
    "agent.work",
    kind="content",
    viewer_write="a viewer may only send the deciding actions (each task gated as its own action route)",
)
def bulk_tasks(request: HttpRequest, payload: BulkTaskIn) -> list[BulkTaskResultOut]:
    """The same PATCH and/or action to each key, each in its own transaction and
    under its own gate, so one task it may not change does not undo the rest."""
    out = []
    patch = payload.patch.model_dump(exclude_unset=True) if payload.patch else None
    for key in dict.fromkeys(k.strip().upper() for k in payload.keys):
        try:
            with transaction.atomic():
                task = task_or_404(request, key)
                if patch:
                    if not may_edit_task(request, task):
                        raise HttpError(403, "you may not change this task")
                    task = apply_task_patch(request, task, dict(patch))
                if payload.action is not None:
                    task = act_and_record(request, task, payload.action).task
                    task = work.task_by_key(key)
            out.append(BulkTaskResultOut(key=key, ok=True, task=AgentTaskOut.model_validate(task)))
        except HttpError as exc:
            out.append(BulkTaskResultOut(key=key, ok=False, error=str(exc)))
    return out


@router.post("/tasks/{key}/relations/", response=TaskDetailOut, summary="Relate a task to another")
# an editor of its workspace, its assignee person, or its agent's own login
@gate("agent.work", "agent-login", "self", kind="content")
def add_task_relation(request: HttpRequest, key: str, payload: TaskRelationIn) -> TaskDetailOut:
    """`blocks` / `blocked_by` / `related` / `duplicate` the task `other` names."""
    task = task_or_404(request, key)
    if not may_edit_task(request, task):
        raise HttpError(403, "relating a task requires being able to change it")
    other = task_or_404(request, payload.other)
    try:
        work.add_relation(task, payload.kind, other, by_user=request.user)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return task_detail(request, task)


@router.delete("/tasks/{key}/relations/{kind}/{other}", response=TaskDetailOut, summary="Remove a relation")
# an editor of its workspace, its assignee person, or its agent's own login
@gate("agent.work", "agent-login", "self", kind="content")
def remove_task_relation(request: HttpRequest, key: str, kind: str, other: str) -> TaskDetailOut:
    task = task_or_404(request, key)
    if not may_edit_task(request, task):
        raise HttpError(403, "relating a task requires being able to change it")
    if not work.remove_relation(task, kind, task_or_404(request, other), by_user=request.user):
        raise HttpError(404, f"{task.key} has no {kind} relation to {other}")
    return task_detail(request, task)


# ---- Triage ------------------------------------------------------------------


@router.get("/triage/", response=list[TriageOut],
            summary="What waits for a decision: suggested tasks, ideas and proposed projects")
@gate("member")
def list_triage(request: HttpRequest, workspace: str = "", project: str = "",
                limit: int = DEFAULT_LIMIT_CAP) -> list[TriageOut]:
    """Oldest first. A snoozed task stays out until its date (or until something
    happens on it). A proposed project's own decision card is shown as the
    project, not as a task. `project` (an address) narrows to one project."""
    listed = listed_agent_workspaces(request)
    scope = [workspace] if workspace and workspace in listed else list(listed)
    tasks = services.filter_tasks(
        work.task_rows(AgentTask.objects.filter(workspace_id__in=scope, status=AgentTask.SUGGESTED)),
        user=request.user, project=project, snoozed="hide",
    ).filter(Q(origin_ref__kind__isnull=True) | ~Q(origin_ref__kind=proposals.DECISION))
    stances = {r["task_id"]: r for r in ProjectBacking.objects.filter(task__in=tasks).values("task_id").annotate(
        b=Count("id", filter=~Q(stance=ProjectBacking.PASSES)), p=Count("id", filter=Q(stance=ProjectBacking.PASSES)))}
    items = [TriageOut(
        kind="idea" if t.kind == AgentTask.IDEA else "task", ref=t.key, title=t.title, why=t.rationale,
        proposed_by=(t.created_by_agent.slug if t.created_by_agent_id else
                     (t.created_by_user.email if t.created_by_user_id else "")),
        workspace=t.workspace_id, project_address=t.project_address, project_name=t.project_name,
        url=t.url, created_at=t.created_at, backers=stances.get(t.pk, {}).get("b", 0),
        passes=stances.get(t.pk, {}).get("p", 0), ask_kind=t.ask_kind, confidence=t.confidence,
        priority=t.priority, human_why=t.human_why) for t in tasks.order_by("created_at", "id")[:clamp_limit(limit)]]
    if not project:
        projects = list(AgentProject.objects.filter(workspace_id__in=scope, status=AgentProject.PROPOSED)
                        .select_related("agent").annotate(
                            b=Count("backings", filter=~Q(backings__stance=ProjectBacking.PASSES)),
                            p=Count("backings", filter=Q(backings__stance=ProjectBacking.PASSES))))
        for p in projects:
            card = proposals.decision_card(p)
            items.append(TriageOut(
                kind="project", ref=p.address, title=p.name, why=card.rationale if card else p.outcome,
                proposed_by=p.agent_slug or "", workspace=p.workspace_id, project_address=p.address,
                project_name=p.name, url=p.url, created_at=p.proposed_at or p.created_at, backers=p.b,
                passes=p.p, ask_kind="review"))
    items.sort(key=lambda i: i.created_at)
    return items[:clamp_limit(limit)]


# ---- labels ------------------------------------------------------------------


def _ws_for_write(request: HttpRequest, workspace: str) -> str:
    ws = (workspace or "").strip() or getattr(request, "workspace_slug", None) or ""
    if not ws:
        found = wsvc.creation_workspace(request)
        if found is None:
            raise HttpError(422, wsvc.creation_refusal(request))
        ws = found.slug
    if ws not in visible_agent_workspaces(request):
        raise HttpError(404, f"workspace '{ws}' not found")
    return ws


def _label_or_404(request: HttpRequest, label_id: int) -> Label:
    label = Label.objects.filter(pk=label_id).first()
    if label is None or label.workspace_id not in visible_agent_workspaces(request):
        raise HttpError(404, f"label {label_id} not found")
    return label


@router.get("/labels/", response=list[LabelOut], summary="The workspace's task labels")
@gate("member")
def list_labels(request: HttpRequest) -> list[LabelOut]:
    return [LabelOut.model_validate(lb) for lb in work.labels_in(listed_agent_workspaces(request))]


@router.post("/labels/", response={201: LabelOut}, summary="Create a task label")
@gate("agent.work", kind="content")
def create_label(request: HttpRequest, payload: LabelIn) -> Status:
    ws = _ws_for_write(request, payload.workspace)
    if not perms.can(request.user, ws, perms.AGENT_WORK):
        raise HttpError(403, "creating a label requires the editor role")
    try:
        label = work.create_label(ws, payload.model_dump())
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, LabelOut.model_validate(label))


@router.patch("/labels/{label_id}", response=LabelOut, summary="Rename or recolour a task label")
@gate("agent.work", kind="content")
def update_label(request: HttpRequest, label_id: int, payload: LabelPatch) -> LabelOut:
    label = _label_or_404(request, label_id)
    if not perms.can(request.user, label.workspace_id, perms.AGENT_WORK):
        raise HttpError(403, "changing a label requires the editor role")
    try:
        return LabelOut.model_validate(work.update_label(label, payload.model_dump(exclude_unset=True)))
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc


@router.delete("/labels/{label_id}", response={204: None}, summary="Delete a task label")
@gate("agent.work", kind="content")
def delete_label(request: HttpRequest, label_id: int):
    """Its tasks keep everything else; they just lose the label."""
    label = _label_or_404(request, label_id)
    if not perms.can(request.user, label.workspace_id, perms.AGENT_WORK):
        raise HttpError(403, "deleting a label requires the editor role")
    label.delete()
    return Status(204, None)


# ---- saved views -------------------------------------------------------------


def _view_out(view: SavedView, user) -> SavedViewOut:
    return SavedViewOut(id=view.pk, workspace=view.workspace_id, name=view.name, shared=view.shared,
                        mine=view.owner_id == user.pk, owner_email=view.owner.email if view.owner_id else None,
                        filters=view.filters or {}, group_by=view.group_by, sort=view.sort, layout=view.layout,
                        rank=view.rank)


def _own_view_or_404(request: HttpRequest, view_id: int) -> SavedView:
    view = SavedView.objects.filter(pk=view_id, owner=request.user).first()
    if view is None or view.workspace_id not in visible_agent_workspaces(request):
        raise HttpError(404, f"view {view_id} not found")
    return view


@router.get("/views/", response=list[SavedViewOut], summary="Your saved task views, and the shared ones")
@gate("member")
def list_views(request: HttpRequest) -> list[SavedViewOut]:
    return [_view_out(v, request.user) for v in work.views_for(request.user, listed_agent_workspaces(request))]


@router.post("/views/", response={201: SavedViewOut}, summary="Save a task view")
# a member saves their own view; sharing it with the workspace is the editor's
@gate("member", "agent.work", kind="content",
      viewer_write="a saved view is the member's own list query; only an editor may share one")
def create_view(request: HttpRequest, payload: SavedViewIn) -> Status:
    ws = _ws_for_write(request, payload.workspace)
    if payload.shared and not perms.can(request.user, ws, perms.AGENT_WORK):
        raise HttpError(403, "sharing a view with the workspace requires the editor role")
    try:
        view = work.save_view(SavedView(workspace_id=ws, owner=request.user), payload.model_dump())
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, _view_out(view, request.user))


@router.patch("/views/{view_id}", response=SavedViewOut, summary="Change a saved task view of yours")
@gate("member", "agent.work", kind="content",
      viewer_write="a saved view is the member's own list query; only an editor may share one")
def update_view(request: HttpRequest, view_id: int, payload: SavedViewPatch) -> SavedViewOut:
    view = _own_view_or_404(request, view_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("shared") and not perms.can(request.user, view.workspace_id, perms.AGENT_WORK):
        raise HttpError(403, "sharing a view with the workspace requires the editor role")
    try:
        return _view_out(work.save_view(view, data), request.user)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc


@router.delete("/views/{view_id}", response={204: None}, summary="Delete a saved task view of yours")
@gate("member", kind="content", viewer_write="a member deletes only their own saved view")
def delete_view(request: HttpRequest, view_id: int):
    _own_view_or_404(request, view_id).delete()
    return Status(204, None)


# ---- a workspace's task settings ---------------------------------------------


def _settings_out(ts) -> TaskSettingsOut:
    return TaskSettingsOut(workspace=ts.workspace_id, prefix=ts.prefix,
                           status_names=work.status_names(ts.workspace_id),
                           update_cadence_days=ts.update_cadence_days)


@router.get("/task-settings/", response=TaskSettingsOut,
            summary="A workspace's task settings: key prefix, status names, update cadence")
@gate("member")
def get_task_settings(request: HttpRequest, workspace: str = "") -> TaskSettingsOut:
    ws = _ws_for_write(request, workspace)
    return _settings_out(work.settings_for(ws))


@router.patch("/task-settings/", response=TaskSettingsOut, summary="Change a workspace's task settings")
@gate("agent.work", setting=True)
def set_task_settings(request: HttpRequest, payload: TaskSettingsPatch, workspace: str = "") -> TaskSettingsOut:
    """The prefix only while no task carries the current one; a status name per
    category (blank restores the default); how many days an active project
    may go without an update before it shows "update missing"."""
    ws = _ws_for_write(request, workspace)
    if not perms.can(request.user, ws, perms.AGENT_WORK):
        raise HttpError(403, "changing task settings requires the editor role")
    before = _settings_out(work.settings_for(ws)).model_dump()
    try:
        out = _settings_out(work.set_settings(ws, payload.model_dump(exclude_unset=True)))
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    audit.note_change(scope=f"workspace:{ws}/task_settings", before=before, after=out.model_dump(),
                      workspace=Workspace.objects.get(slug=ws))
    return out


# ---- a project's milestones and updates --------------------------------------


@router.get("/workspace-projects/{workspace}/{handle}/milestones/", response=list[MilestoneOut],
            summary="A project's milestones, each with its progress")
@gate("member")
def list_milestones(request: HttpRequest, workspace: str, handle: str) -> list[MilestoneOut]:
    project = project_ws_or_404(request, workspace, handle)
    return [MilestoneOut.model_validate(m) for m in work.milestones_of(project)]


@router.post("/workspace-projects/{workspace}/{handle}/milestones/", response={201: MilestoneOut},
             summary="Add a milestone to a project")
# an editor of its workspace or its lead agent's, its lead, or a working agent's own login
@gate("agent.work", "agent-login", "self", kind="content")
def create_milestone(request: HttpRequest, workspace: str, handle: str, payload: MilestoneIn) -> Status:
    project = project_ws_or_404(request, workspace, handle)
    require_project_edit(request, project)
    try:
        m = work.save_milestone(project, payload.model_dump())
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, MilestoneOut.model_validate(next(x for x in work.milestones_of(project) if x.pk == m.pk)))


def _milestone_or_404(project, milestone_id: int):
    m = project.milestones.filter(pk=milestone_id).first()
    if m is None:
        raise HttpError(404, f"milestone {milestone_id} not found")
    return m


@router.patch("/workspace-projects/{workspace}/{handle}/milestones/{milestone_id}", response=MilestoneOut,
              summary="Change a project's milestone")
# an editor of its workspace or its lead agent's, its lead, or a working agent's own login
@gate("agent.work", "agent-login", "self", kind="content")
def update_milestone(request: HttpRequest, workspace: str, handle: str, milestone_id: int,
                     payload: MilestonePatch) -> MilestoneOut:
    project = project_ws_or_404(request, workspace, handle)
    require_project_edit(request, project)
    try:
        m = work.save_milestone(project, payload.model_dump(exclude_unset=True), _milestone_or_404(project, milestone_id))
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return MilestoneOut.model_validate(next(x for x in work.milestones_of(project) if x.pk == m.pk))


@router.delete("/workspace-projects/{workspace}/{handle}/milestones/{milestone_id}", response={204: None},
               summary="Delete a project's milestone (its tasks stay, without it)")
# an editor of its workspace or its lead agent's, its lead, or a working agent's own login
@gate("agent.work", "agent-login", "self", kind="content")
def delete_milestone(request: HttpRequest, workspace: str, handle: str, milestone_id: int):
    project = project_ws_or_404(request, workspace, handle)
    require_project_edit(request, project)
    _milestone_or_404(project, milestone_id).delete()
    return Status(204, None)


@router.get("/workspace-projects/{workspace}/{handle}/updates/", response=list[ProjectUpdateOut],
            summary="A project's status updates, newest first (drafts too, for those who may publish them)")
@gate("member")
def list_project_updates(request: HttpRequest, workspace: str, handle: str,
                         limit: int = 50) -> list[ProjectUpdateOut]:
    project = project_ws_or_404(request, workspace, handle)
    rows = project.updates.select_related("author_user", "author_agent", "published_by")
    if not may_edit_project(request, project):
        rows = rows.filter(status=ProjectUpdate.PUBLISHED)
    return [ProjectUpdateOut.model_validate(u) for u in rows[:clamp_limit(limit)]]


@router.post("/workspace-projects/{workspace}/{handle}/updates/", response={201: ProjectUpdateOut},
             summary="Post a project update: its health and a short note")
# an editor of its workspace or its lead agent's, its lead, or a working agent's own login
@gate("agent.work", "agent-login", "self", kind="content")
def post_project_update(request: HttpRequest, workspace: str, handle: str, payload: ProjectUpdateIn) -> Status:
    """A person's update is published (unless `draft`). An agent posting under
    its own login always writes a draft — the lead publishes it, choosing the
    health: the agent proposes, the person decides."""
    project = project_ws_or_404(request, workspace, handle)
    require_project_edit(request, project)
    agents = [a for a in [project.agent, *project.member_agents.all()] if a is not None]
    author_agent = next((a for a in agents if a.is_own_login(request.user)), None)
    run = None
    if payload.checkin_run is not None:
        run = project.checkin_runs.filter(pk=payload.checkin_run).first()
        if run is None:
            raise HttpError(422, f"check-in run {payload.checkin_run} is not {project.name}'s")
    update = work.post_update(project, health=payload.health, body=payload.body, draft=payload.draft,
                              author_user=None if author_agent else request.user, author_agent=author_agent,
                              checkin_run=run)
    return Status(201, ProjectUpdateOut.model_validate(update))


@router.post("/workspace-projects/{workspace}/{handle}/updates/{update_id}/publish", response=ProjectUpdateOut,
             summary="Publish a drafted project update, choosing its health")
# a person who may change the project; never an agent's own login (it drafts, a person decides)
@gate("agent.work", "self", kind="content")
def publish_project_update(request: HttpRequest, workspace: str, handle: str, update_id: int,
                           payload: ProjectUpdatePublishIn) -> ProjectUpdateOut:
    project = project_ws_or_404(request, workspace, handle)
    require_project_edit(request, project)
    agents = [a for a in [project.agent, *project.member_agents.all()] if a is not None]
    if any(a.is_own_login(request.user) for a in agents):
        raise HttpError(403, "an agent drafts its project's update; a person publishes it")
    update = project.updates.filter(pk=update_id).first()
    if update is None:
        raise HttpError(404, f"update {update_id} not found")
    try:
        update = work.publish_update(update, by_user=request.user, health=payload.health, body=payload.body)
    except ValueError as exc:
        raise HttpError(409, str(exc)) from exc
    return ProjectUpdateOut.model_validate(update)


# ---- what a project serves to the workspace ----------------------------------


@router.get("/workspace-projects/{workspace}/{handle}/services/", response=list[ProjectServiceOut],
            summary="What a project serves to its workspace: its knowledge base, shared MCP servers, skills, plugins")
@gate("member")
def list_project_services(request: HttpRequest, workspace: str, handle: str) -> list[ProjectServiceOut]:
    project = project_ws_or_404(request, workspace, handle)
    return [ProjectServiceOut(**row) for row in work.services_of(project, origin=base_url())]


@router.post("/workspace-projects/{workspace}/{handle}/services/", response={201: ProjectServiceOut},
             summary="Share an MCP server, a skill or a plugin with the workspace, from this project")
# an editor of its workspace or its lead agent's, its lead, or a working agent's own login
@gate("agent.work", "agent-login", "self", kind="content")
def add_project_service(request: HttpRequest, workspace: str, handle: str, payload: ProjectServiceIn) -> Status:
    """Listed on the project page and, for an MCP server, in the workspace's
    directory (`GET /api/mcp-servers/`). canopy does not proxy or vet a remote
    server: its URL is what members add to their own Claude Code."""
    project = project_ws_or_404(request, workspace, handle)
    require_project_edit(request, project)
    try:
        svc = work.save_service(project, payload.model_dump(), by_user=request.user)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, next(ProjectServiceOut(**r) for r in work.services_of(project, origin=base_url())
                            if r["id"] == svc.pk))


def _service_or_404(project, service_id: int):
    svc = project.served.filter(pk=service_id).first()
    if svc is None:
        raise HttpError(404, f"{project.address} serves nothing with id {service_id}")
    return svc


@router.patch("/workspace-projects/{workspace}/{handle}/services/{service_id}", response=ProjectServiceOut,
              summary="Change a shared MCP server, skill or plugin")
# an editor of its workspace or its lead agent's, its lead, or a working agent's own login
@gate("agent.work", "agent-login", "self", kind="content")
def update_project_service(request: HttpRequest, workspace: str, handle: str, service_id: int,
                           payload: ProjectServicePatch) -> ProjectServiceOut:
    project = project_ws_or_404(request, workspace, handle)
    require_project_edit(request, project)
    try:
        svc = work.save_service(project, payload.model_dump(exclude_unset=True), _service_or_404(project, service_id))
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return next(ProjectServiceOut(**r) for r in work.services_of(project, origin=base_url()) if r["id"] == svc.pk)


@router.delete("/workspace-projects/{workspace}/{handle}/services/{service_id}", response={204: None},
               summary="Stop sharing an MCP server, skill or plugin")
# an editor of its workspace or its lead agent's, its lead, or a working agent's own login
@gate("agent.work", "agent-login", "self", kind="content")
def remove_project_service(request: HttpRequest, workspace: str, handle: str, service_id: int):
    """A knowledge base is not removed here: it has its own version log."""
    project = project_ws_or_404(request, workspace, handle)
    require_project_edit(request, project)
    svc = _service_or_404(project, service_id)
    if svc.kind == "kb":
        raise HttpError(422, "a knowledge base is not removed here")
    svc.delete()
    return Status(204, None)
