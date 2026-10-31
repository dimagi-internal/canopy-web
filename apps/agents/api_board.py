"""/api/agents/{slug}/… — an agent's board: projects, tasks and the action queue.

Tasks and projects are the workspace's (`tasks_api` is their own address);
these routes are an agent's VIEW of them, kept so the fleet names work as it
always has: a task by its key or this agent's alias ("T12"), a project by its
handle or this agent's alias ("P5")."""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Case, IntegerField, Q, When
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.pagination import clamp_limit
from apps.api.route_gates import gate
from apps.contacts import services as contacts
from apps.harness import turn_access
from apps.harness.models import Turn, agent_turns_q
from apps.workspaces.links import base_url
from apps.workspaces import permissions as perms

from . import engagement, ideas, participants, proposals, services, work
from .credentials import workspace_vaults
from .gates import (
    agent_for_read,
    agent_for_work,
    agent_in_project_tree,
    check_task_visible,
    may_edit_project,
    project_or_404,
    require_project_edit,
    require_task_edit,
    visible_agent_workspaces,
)
from .models import AgentTask
from .proposals_api import resolve_task_ref
from .schemas import (
    ActionAppliedIn,
    ActOut,
    AgentProjectDetailOut,
    AgentProjectIn,
    AgentProjectOut,
    AgentProjectPatch,
    AgentTaskActionIn,
    AgentTaskActionOut,
    AgentTaskDetailOut,
    AgentTaskIn,
    AgentTaskOut,
    AgentTaskPatch,
    MilestoneOut,
    ProjectParticipantOut,
    ProjectProgressOut,
    ProjectServiceOut,
    ProjectUpdateOut,
    ProjectVaultOut,
    ProposedTaskOut,
    TurnBriefOut,
)

logger = logging.getLogger(__name__)

router = Router(auth=session_auth, tags=["agents"])


# ---- projects (the work a `Projects/<name>` Drive folder holds) ----


@router.get("/{slug}/projects/", response=list[AgentProjectOut],
            summary="List the agent's projects",)
@gate("member")
def list_projects(request: HttpRequest, slug: str, status: str = "") -> list[AgentProjectOut]:
    agent = agent_for_read(request, slug)
    projects = services.list_projects(agent, status=status)
    work.annotate_projects(projects)
    return [AgentProjectOut.model_validate(p) for p in projects]


@router.post("/{slug}/projects/", response={201: AgentProjectOut}, summary="Create a project",)
@gate("agent.work", kind="content")
def create_project(request: HttpRequest, slug: str, payload: AgentProjectIn) -> Status:
    agent = agent_for_work(request, slug)
    try:
        project = services.create_project(agent, payload)
    except services.UnknownPersonError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, AgentProjectOut.model_validate(project))


#: How many of the agent's turns a project page shows.
_PROJECT_RECENT_TURNS = 10


def _turns_touching(agent, ext_ids: list[str]) -> list:  # noqa: C901
    """The agent's latest turns whose `task_ext_ids` name any of `ext_ids`.

    `task_ext_ids` is a JSON list. Postgres answers "contains this element"
    natively; SQLite (the test database) has no such lookup, so there the
    element is matched as its quoted JSON text — exact, since `"T1"` cannot
    match inside `"T12"`.
    """
    from django.db import connection

    if not ext_ids:
        return []
    q = Q()
    for eid in ext_ids:
        if connection.features.supports_json_field_contains:
            q |= Q(task_ext_ids__contains=[eid])
        else:
            q |= Q(task_ext_ids__icontains=f'"{eid}"')
    # `redact` reads each turn's agent and claiming box; join them up front.
    return list(Turn.objects.filter(agent_turns_q(agent)).filter(q)
                .select_related("agent", "claimed_by")
                .order_by("-created_at")[:_PROJECT_RECENT_TURNS])


def _turn_brief(turn) -> TurnBriefOut:
    preview = (turn.report_title or turn.prompt or "").strip()
    return TurnBriefOut(id=turn.id, status=turn.status, prompt_preview=preview[:200],
                        created_at=turn.created_at, task_ext_ids=list(turn.task_ext_ids or []))


@router.get("/{slug}/projects/{ref}/", response=AgentProjectDetailOut,
            summary="Get one project: its fields and links, its tasks, its recent turns",)
@gate("member")
def get_project(request: HttpRequest, slug: str, ref: str) -> AgentProjectDetailOut:
    agent = agent_for_read(request, slug)
    return project_detail(request, project_or_404(agent, ref))


def project_detail(request: HttpRequest, project) -> AgentProjectDetailOut:
    """One project's page — shared by the agent route and the workspace-project route."""

    live_first = Case(When(status__in=services.LIVE_STATUSES, then=0), default=1,
                      output_field=IntegerField())
    tasks = list(work.task_rows(project.tasks).order_by(live_first, "rank", "id"))
    project._task_count = len(tasks)
    project._open_task_count = sum(t.status in services.LIVE_STATUSES for t in tasks)
    project._done_task_count = sum(t.status == AgentTask.DONE for t in tasks)
    # `waiting_task_count` is left to the model property: one COUNT through
    # `services.waiting_q`, rather than a second Python copy of that predicate.
    # Everyone sees what the agent did; a turn's prompt is a log (turn_access).
    # A turn names the tasks it touched by its agent's alias or their key, so
    # they are found per agent that did or filed the work.
    by_agent: dict = {}
    for t in tasks:
        for a in {t.agent, t.created_by_agent} - {None}:
            by_agent.setdefault(a.pk, (a, []))[1].extend(x for x in (t.ext_id, t.key) if x)
    found = [turn for a, eids in by_agent.values() for turn in _turns_touching(a, eids)]
    found = sorted({turn.pk: turn for turn in found}.values(), key=lambda turn: turn.created_at,
                   reverse=True)[:_PROJECT_RECENT_TURNS]
    turns = turn_access.redact(found, request.user)
    # Built from the plain project shape: `project.tasks` is a manager, not a list.

    work.annotate_projects([project])
    editable = may_edit_project(request, project)
    return AgentProjectDetailOut(
        **AgentProjectOut.model_validate(project).model_dump(),
        tasks=[AgentTaskOut.model_validate(t) for t in tasks],
        recent_turns=[_turn_brief(t) for t in turns],
        # Same gate as the rest of the project: members of the agent's workspace.
        participants=[ProjectParticipantOut(**p) for p in participants.of_project(project)],
        proposal=(proposals.card(project, request.user) if proposals.is_proposal(project) else None),
        proposed_tasks=[ProposedTaskOut(**card) for card in proposals.task_cards(
            list(proposals.proposed_tasks_of(project).filter(status="suggested")), request.user)],
        board_readable=project.workspace_id in visible_agent_workspaces(request),
        progress=ProjectProgressOut(**work.progress_of(project)),
        milestones=[MilestoneOut.model_validate(m) for m in work.milestones_of(project)],
        latest_update=(ProjectUpdateOut.model_validate(latest) if (latest := work.latest_update(project)) else None),
        draft_updates=([ProjectUpdateOut.model_validate(u) for u in project.updates.filter(status="draft")
                        .select_related("author_user", "author_agent")] if editable else []),
        may_edit=editable,
        services=[ProjectServiceOut(**row) for row in work.services_of(project, origin=base_url())],
        vaults=[ProjectVaultOut(**row) for row in workspace_vaults.project_vaults(project, request.user)],
    )


@router.patch("/{slug}/projects/{ref}/", response=AgentProjectOut, summary="Update a project",)
@gate("agent.work", kind="content")
def patch_project(request: HttpRequest, slug: str, ref: str,
                  payload: AgentProjectPatch) -> AgentProjectOut:
    agent = agent_for_read(request, slug)
    project = project_or_404(agent, ref)
    require_project_edit(request, project)
    data = payload.model_dump(exclude_unset=True)
    if "links" in data and data["links"] is not None:
        data["links"] = [link if isinstance(link, dict) else link.model_dump()
                         for link in data["links"]]
    return AgentProjectOut.model_validate(apply_project_patch(request, project, data))


def require_workspace_editor(request: HttpRequest, workspace: str, steward=None) -> None:
    """Placing a project in a workspace other than its lead agent's makes it
    that workspace's, so the caller must be able to edit there too — writing
    the lead agent is not enough. Same 404 as a missing workspace for a
    non-member."""
    if not workspace or (steward is not None and workspace == steward.workspace_id):
        return
    if not perms.can(request.user, workspace, perms.CONTENT_WRITE):
        raise HttpError(404, f"workspace '{workspace}' not found")


def apply_project_patch(request: HttpRequest, project, data: dict):
    """A PATCH body against one project, from either address. `handle` promotes
    it to a workspace project; `steward` hands a workspace project on — and the
    caller must be able to write the new steward too."""
    if "spawned_from" in data:

        ref_ = (data.pop("spawned_from") or "").strip()
        project.spawned_from = resolve_task_ref(request, ref_) if ref_ else None
    handle, steward = data.pop("handle", None), data.pop("steward", None)
    members = data.pop("member_agents", None)
    workspace = (data.pop("workspace", None) or "").strip()
    if workspace and handle is None:
        handle = project.handle
    if workspace and workspace != project.workspace_id:
        require_workspace_editor(request, workspace)
    try:
        with transaction.atomic():
            if handle is not None:
                project = services.promote_project(project, handle, workspace)
            if steward is not None:
                steward = steward.strip()
                project = services.set_steward(project, agent_for_work(request, steward) if steward else None)
            if members is not None:
                project = services.set_member_agents(
                    project, [agent_in_project_tree(project, m.strip()) for m in members if m.strip()])
            project = services.patch_project(project, data)
    except services.UnknownPersonError as exc:
        raise HttpError(422, str(exc)) from exc
    except services.ProjectScopeError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return project


# ---- tasks, by key or this agent's alias ----


def _get_task_or_404(agent, ref: str, request: HttpRequest | None = None):
    task = services.get_task(agent, ref)
    if task is None:
        raise HttpError(404, f"task {ref} not found")
    if request is not None:
        check_task_visible(request, task, ref)
    return task


@router.get("/{slug}/tasks/", response=list[AgentTaskOut], summary="List the agent's tasks",)
@gate("member")
def list_tasks(request: HttpRequest, slug: str, project: str = "", status: str = "",
               waiting: str = "", ask: str = "", batch: str = "",
               parent: str = "") -> list[AgentTaskOut]:
    """Filters: `project` (an ext_id, or `none` for one-offs), `status`
    (comma-separated), `waiting=me` (open asks nobody owns plus tasks parked on
    you), `ask=open|closed`, `batch` (a batch_key), `parent` (an ext_id for
    that task's direct subtasks, or `none` for top-level tasks only)."""
    agent = agent_for_read(request, slug)
    qs = services.filter_tasks(
        work.task_rows(AgentTask.objects.filter(services.agent_tasks_q(agent))),
        user=request.user, project=project, status=status, waiting=waiting, ask=ask, batch=batch,
        parent=parent)
    return [AgentTaskOut.model_validate(t) for t in qs]


@router.post("/{slug}/tasks/", response={201: list[AgentTaskOut]},
             summary="Create tasks (a list; an idempotency_key replays instead of duplicating)",)
@gate("agent.work", kind="content")  # or the agent's own login
def create_tasks(request: HttpRequest, slug: str, payload: list[AgentTaskIn]) -> Status:
    agent = agent_for_read(request, slug)
    # A task can carry `on_approve` — work that runs the moment a viewer approves
    # it — so WRITING one is the reshaping tier: an editor, or the agent itself
    # under its own login.
    if not agent.is_own_login(request.user):
        agent = agent_for_work(request, slug)
    try:
        tasks = services.create_tasks(agent, [p.model_dump(exclude_unset=True) for p in payload],
                                      by_user=request.user)
    except services.UnknownPersonError as exc:
        raise HttpError(422, str(exc)) from exc
    except services.DuplicateTaskError as exc:
        # An explicit `ext_id` the agent already uses, with no idempotency key
        # to say "this is the same task".
        raise HttpError(409, f"{exc} — omit ext_id, or send an idempotency_key to replay") from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, [AgentTaskOut.model_validate(t) for t in tasks])


@router.get("/{slug}/tasks/{ref}/", response=AgentTaskDetailOut,
            summary="Get one task with everything done to it",)
@gate("member")
def get_task(request: HttpRequest, slug: str, ref: str) -> AgentTaskDetailOut:
    agent = agent_for_read(request, slug)
    task = _get_task_or_404(agent, ref, request)
    # Built from the plain task shape: `task.actions` is a manager, not a list.
    return AgentTaskDetailOut(
        **AgentTaskOut.model_validate(task).model_dump(),
        actions=[AgentTaskActionOut.model_validate(a)
                 for a in task.actions.select_related("agent", "task")],
    )


@router.patch("/{slug}/tasks/{ref}/", response=AgentTaskOut, summary="Update a task's fields",)
@gate("agent.work", kind="content")
def patch_task(request: HttpRequest, slug: str, ref: str, payload: AgentTaskPatch) -> AgentTaskOut:
    agent = agent_for_read(request, slug)
    task = _get_task_or_404(agent, ref, request)
    require_task_edit(request, task)
    data = payload.model_dump(exclude_unset=True)
    return AgentTaskOut.model_validate(apply_task_patch(request, task, data, route_agent=agent))


def apply_task_patch(request: HttpRequest, task, data: dict, *, route_agent=None):
    """A PATCH body against one task, from either address. Giving it to an
    agent is the editor's (or that agent claiming it under its own login)."""
    assignee = (data.get("assignee") or "").strip() if data.get("assignee") is not None else None
    if assignee and "@" not in assignee:
        slug_ = assignee.removeprefix("agent:")
        target = agent_for_read(request, slug_, honor_pin=False)
        if not target.is_own_login(request.user):
            agent_for_work(request, slug_, honor_pin=False,
                           forbidden="giving a task to an agent requires the editor role")
    by_agent = next((a for a in (route_agent, task.agent, task.created_by_agent)
                     if a is not None and a.is_own_login(request.user)), None)
    try:
        return services.patch_task(task, data, by_user=request.user, by_agent=by_agent)
    except (services.UnknownPersonError, ValueError) as exc:
        # TaskTreeError, AssigneeError, and a quote whose person_id is not known here.
        raise HttpError(422, str(exc)) from exc


#: Actions that RESHAPE work rather than answer it: `nudge` starts an agent turn
#: on work already under way (the QuickTurn tier) and `done` declares it
#: finished. approve / decline / reply answer what is already on the board — the
#: viewer tier ("decide, read the board").
_EDITOR_ACTIONS = frozenset({"nudge", "done"})

#: Task actions on a project's task that count as a person engaging with the project.
_ENGAGEMENT_OF_ACTION = {"approve": "approved", "decline": "declined", "reply": "commented"}


def _engaged_with_project_task(request: HttpRequest, task, action: str) -> None:
    """Record what a person just did to one of a project's tasks (`EngagementEvent`),
    and make a person who replied a participant of the project. Never fails the
    action: both writes are best-effort."""

    kind = _ENGAGEMENT_OF_ACTION.get(action)
    if kind is None:
        return
    engagement.record(kind, workspace=task.workspace_id, surface=engagement.surface_of(request),
                      user=request.user, project=task.project, task=task)
    if action == "reply":
        try:
            person = contacts.person_for(user=request.user)
            participants.ensure(task.project, person, source="reply")
        except Exception:  # noqa: BLE001 — a participant is never worth a failed reply
            logger.exception("could not add %s as a participant of %s", request.user.pk, task.project_id)


@router.post("/{slug}/tasks/{ref}/actions", response=ActOut,
             summary="Act on a task: approve, decline, reply, nudge or done",)
# approve/decline/reply = member; dispatch/done = editor, or the idea's own person marking a kind=idea task done
# (shipped)
@gate(
    "member",
    "agent.work",
    kind="content",
    viewer_write=(
        "approve/decline/reply answer what is already on the board (interaction tier); dispatch/done require "
        "agent.work — except that the person whose words an idea task carries may mark it done (shipped): it is their "
        "idea"
    ),
)
def act_on_task(request: HttpRequest, slug: str, ref: str, payload: AgentTaskActionIn) -> ActOut:
    """approve → in progress, and the agent starts now: its `on_approve` turns,
    or one turn written from the card when it has none. decline → declined (the
    comment is the reason). reply → on an open question, the answer (closes the
    ask; runs `on_approve`, or with none starts one turn carrying the answer —
    whoever answers, unless it is the agent itself); on anything else a note —
    an editor's note starts a turn carrying it, a viewer's waits for the
    agent's next turn. nudge (editor, in-progress tasks) → start a turn on it
    now, status unchanged. done → done (editor). 409 when the task is not in a
    state the action applies to (an ask already closed, a finished task,
    nudging one that is not in progress); `turn_ids` names the turns the action
    started."""
    agent = agent_for_read(request, slug)
    task = _get_task_or_404(agent, ref, request)
    return act_and_record(request, task, payload)


def act_and_record(request: HttpRequest, task, payload: AgentTaskActionIn) -> ActOut:
    """One action on one task, from either address."""
    if payload.action in _EDITOR_ACTIONS:
        # The person whose idea it is may call it shipped, as its owner may
        # (hal T82); a task's own assignee may finish it.
        if not ((payload.action == "done" and ideas.is_ideas_person(task, request.user))
                or (payload.action == "done" and task.assignee_user_id == request.user.pk)):
            if task.agent_id is not None:
                agent_for_work(request, task.agent.slug, honor_pin=False)
            else:
                require_task_edit(request, task)
    doer = task.agent
    own = doer is not None and doer.is_own_login(request.user)
    try:
        task, row, turns = services.act(
            task, action=payload.action, comment=payload.comment,
            by=request.user.email or request.user.get_username(), by_user=request.user,
            actor_workspace_ids=visible_agent_workspaces(request),
            # Who may start a turn is the QuickTurn gate (`enqueue_turn` needs
            # write); it decides whether a reply wakes the agent. Never the agent
            # replying on its own card: its note would wake itself, and a turn
            # that replies again would wake it again.
            may_start_turns=(doer is not None
                             and perms.can(request.user, doer.workspace_id, perms.AGENT_WORK)
                             and not own),
            # An ANSWER to an open question wakes the agent whoever writes it (it
            # is the response the agent asked for) — but never the agent itself.
            by_agent_itself=own)
    except services.ClosedAskError as exc:
        raise HttpError(409, str(exc)) from exc
    except PermissionError as exc:
        # A proposed project's decision card, by someone who may not decide it.
        raise HttpError(403, str(exc)) from exc
    except ValueError as exc:
        # An empty reply, or an `on_approve` spec that cannot run. `act` is
        # atomic: the ask is still open and the action can be retried.
        raise HttpError(422, str(exc)) from exc
    if task.project_id is not None and not own:
        _engaged_with_project_task(request, task, payload.action)
    return ActOut(task=AgentTaskOut.model_validate(task),
                  action=AgentTaskActionOut.model_validate(row),
                  turn_ids=[t.id for t in turns])


# ---- the agent's queue: actions it still has to carry out ----


@router.get("/{slug}/actions/", response=list[AgentTaskActionOut],
            summary="List actions on the agent's tasks (the agent drains ?status=pending)",)
@gate("member")
def list_task_actions(request: HttpRequest, slug: str, status: str = "",
                      limit: int = 200) -> list[AgentTaskActionOut]:
    """At most `limit` rows (default 200, cap 500). Unfiltered, PENDING rows come
    first (newest first), then the rest newest first — so a short page still
    carries the agent's whole queue before any history."""
    agent = agent_for_read(request, slug)
    limit = clamp_limit(limit)
    if status == "pending":
        rows = services.pending_actions(agent)  # oldest first: the order to carry them out
    else:
        rows = agent.task_actions.select_related("agent", "task")
        if status:
            rows = rows.filter(status=status).order_by("-created_at", "-id")
        else:
            pending_first = Case(When(status="pending", then=0), default=1,
                                 output_field=IntegerField())
            rows = rows.order_by(pending_first, "-created_at", "-id")
    return [AgentTaskActionOut.model_validate(a) for a in rows[:limit]]


@router.post("/{slug}/actions/{action_id}/applied", response=AgentTaskActionOut,
             summary="Mark an action applied (the agent calls this after carrying it out)",)
@gate("agent.work", kind="content")  # or the agent's own login
def mark_task_action_applied(request: HttpRequest, slug: str, action_id: int,
                             payload: ActionAppliedIn) -> AgentTaskActionOut:
    # Marking an action applied tells the agent it is done, so the agent never
    # carries it out — the agent's own login or the reshaping tier, not any viewer.
    agent = agent_for_read(request, slug)
    if not agent.is_own_login(request.user):
        agent = agent_for_work(request, slug)
    row = agent.task_actions.filter(id=action_id).select_related("agent", "task").first()
    if row is None:
        raise HttpError(404, f"action {action_id} not found")
    return AgentTaskActionOut.model_validate(services.mark_applied(row, payload.result_note))
