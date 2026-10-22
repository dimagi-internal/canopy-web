"""/api/agents/{slug}/… — an agent's board: projects, tasks and the action queue."""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Case, IntegerField, Q, When
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.pagination import clamp_limit
from apps.harness.models import Turn, agent_turns_q
from apps.workspaces import permissions as perms

from . import services
from .gates import (
    agent_for_read,
    agent_for_work,
    project_or_404,
    visible_agent_workspaces,
)
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
    ProjectParticipantOut,
    ProposedTaskOut,
    TurnBriefOut,
)

logger = logging.getLogger(__name__)

router = Router(auth=session_auth, tags=["agents"])


# ---- projects (the work a `Projects/<name>` Drive folder holds) ----


@router.get("/{slug}/projects/", response=list[AgentProjectOut],
            summary="List the agent's projects",)
def list_projects(request: HttpRequest, slug: str, status: str = "") -> list[AgentProjectOut]:
    agent = agent_for_read(request, slug)
    projects = services.list_projects(agent, status=status)
    counts = services.project_task_counts(agent)
    for project in projects:
        (project._task_count, project._open_task_count,
         project._waiting_task_count) = counts.get(project.pk, (0, 0, 0))
    return [AgentProjectOut.model_validate(p) for p in projects]


@router.post("/{slug}/projects/", response={201: AgentProjectOut}, summary="Create a project",)
def create_project(request: HttpRequest, slug: str, payload: AgentProjectIn) -> Status:
    agent = agent_for_work(request, slug)
    try:
        project = services.create_project(agent, payload)
    except services.UnknownPersonError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, AgentProjectOut.model_validate(project))


#: How many of the agent's turns a project page shows.
_PROJECT_RECENT_TURNS = 10


def _turns_touching(agent, ext_ids: list[str]) -> list:
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
def get_project(request: HttpRequest, slug: str, ref: str) -> AgentProjectDetailOut:
    agent = agent_for_read(request, slug)
    return project_detail(request, project_or_404(agent, ref))


def project_detail(request: HttpRequest, project) -> AgentProjectDetailOut:
    """One project's page — shared by the agent route and the workspace-project route."""
    from apps.harness import turn_access

    live_first = Case(When(status__in=services.LIVE_STATUSES, then=0), default=1,
                      output_field=IntegerField())
    tasks = list(project.tasks.select_related("agent", "project", "waiting_on_user", "parent")
                 .prefetch_related("human_whys")
                 .order_by(live_first, "position", "id"))
    project._task_count = len(tasks)
    project._open_task_count = sum(t.status in services.LIVE_STATUSES for t in tasks)
    # `waiting_task_count` is left to the model property: one COUNT through
    # `services.waiting_q`, rather than a second Python copy of that predicate.
    # Everyone sees what the agent did; a turn's prompt is a log (turn_access).
    # ext_ids are per agent, so a workspace project's turns are found per task owner.
    by_agent: dict = {}
    for t in tasks:
        by_agent.setdefault(t.agent_id, (t.agent, []))[1].append(t.ext_id)
    found = [turn for a, eids in by_agent.values() for turn in _turns_touching(a, eids)]
    found = sorted({turn.pk: turn for turn in found}.values(), key=lambda turn: turn.created_at,
                   reverse=True)[:_PROJECT_RECENT_TURNS]
    turns = turn_access.redact(found, request.user)
    # Built from the plain project shape: `project.tasks` is a manager, not a list.
    from . import participants, proposals

    return AgentProjectDetailOut(
        **AgentProjectOut.model_validate(project).model_dump(),
        tasks=[AgentTaskOut.model_validate(t) for t in tasks],
        recent_turns=[_turn_brief(t) for t in turns],
        # Same gate as the rest of the project: members of the agent's workspace.
        participants=[ProjectParticipantOut(**p) for p in participants.of_project(project)],
        proposal=(proposals.card(project, request.user) if proposals.is_proposal(project) else None),
        proposed_tasks=[ProposedTaskOut(**proposals.task_card(t, request.user))
                        for t in proposals.proposed_tasks_of(project).filter(status="suggested")],
        board_readable=project.agent.workspace_id in visible_agent_workspaces(request),
    )


@router.patch("/{slug}/projects/{ref}/", response=AgentProjectOut, summary="Update a project",)
def patch_project(request: HttpRequest, slug: str, ref: str,
                  payload: AgentProjectPatch) -> AgentProjectOut:
    agent = agent_for_work(request, slug)
    project = project_or_404(agent, ref)
    data = payload.model_dump(exclude_unset=True)
    if "links" in data and data["links"] is not None:
        data["links"] = [link if isinstance(link, dict) else link.model_dump()
                         for link in data["links"]]
    return AgentProjectOut.model_validate(apply_project_patch(request, project, data))


def require_workspace_editor(request: HttpRequest, workspace: str, steward) -> None:
    """Placing a project in a workspace other than its steward's makes it that
    workspace's, so the caller must be able to edit there too — writing the
    steward is not enough. Same 404 as a missing workspace for a non-member."""
    if not workspace or workspace == steward.workspace_id:
        return
    if not perms.can(request.user, workspace, perms.CONTENT_WRITE):
        raise HttpError(404, f"workspace '{workspace}' not found")


def apply_project_patch(request: HttpRequest, project, data: dict):
    """A PATCH body against one project, from either address. `handle` promotes
    it to a workspace project; `steward` hands a workspace project on — and the
    caller must be able to write the new steward too."""
    if "spawned_from" in data:
        from .proposals_api import resolve_task_ref

        ref_ = (data.pop("spawned_from") or "").strip()
        project.spawned_from = resolve_task_ref(request, ref_) if ref_ else None
    handle, steward = data.pop("handle", None), data.pop("steward", None)
    workspace = (data.pop("workspace", None) or "").strip()
    if workspace and handle is None:
        raise HttpError(422, "workspace is set with handle, when the project is promoted")
    require_workspace_editor(request, workspace, project.agent)
    try:
        with transaction.atomic():
            if handle is not None:
                project = services.promote_project(project, handle, workspace)
            if steward is not None:
                project = services.set_steward(project, agent_for_work(request, steward.strip()))
            project = services.patch_project(project, data)
    except services.UnknownPersonError as exc:
        raise HttpError(422, str(exc)) from exc
    except services.ProjectScopeError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    return project


# ---- tasks, addressed by `ext_id` ----


def _get_task_or_404(agent, ref: str):
    task = services.get_task(agent, ref)
    if task is None:
        raise HttpError(404, f"task {ref} not found")
    return task


@router.get("/{slug}/tasks/", response=list[AgentTaskOut], summary="List the agent's tasks",)
def list_tasks(request: HttpRequest, slug: str, project: str = "", status: str = "",
               waiting: str = "", ask: str = "", batch: str = "",
               parent: str = "") -> list[AgentTaskOut]:
    """Filters: `project` (an ext_id, or `none` for one-offs), `status`
    (comma-separated), `waiting=me` (open asks nobody owns plus tasks parked on
    you), `ask=open|closed`, `batch` (a batch_key), `parent` (an ext_id for
    that task's direct subtasks, or `none` for top-level tasks only)."""
    agent = agent_for_read(request, slug)
    qs = services.filter_tasks(
        agent.tasks.select_related("agent", "project", "waiting_on_user", "parent")
        .prefetch_related("human_whys"),
        user=request.user, project=project, status=status, waiting=waiting, ask=ask, batch=batch,
        parent=parent)
    return [AgentTaskOut.model_validate(t) for t in qs]


@router.post("/{slug}/tasks/", response={201: list[AgentTaskOut]},
             summary="Create tasks (a list; an idempotency_key replays instead of duplicating)",)
def create_tasks(request: HttpRequest, slug: str, payload: list[AgentTaskIn]) -> Status:
    agent = agent_for_read(request, slug)
    # A task can carry `on_approve` — work that runs the moment a viewer approves
    # it — so WRITING one is the reshaping tier: an editor, or the agent itself
    # under its own login.
    if not agent.is_own_login(request.user):
        agent = agent_for_work(request, slug)
    try:
        tasks = services.create_tasks(agent, [p.model_dump(exclude_unset=True) for p in payload])
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
def get_task(request: HttpRequest, slug: str, ref: str) -> AgentTaskDetailOut:
    agent = agent_for_read(request, slug)
    task = _get_task_or_404(agent, ref)
    # Built from the plain task shape: `task.actions` is a manager, not a list.
    return AgentTaskDetailOut(
        **AgentTaskOut.model_validate(task).model_dump(),
        actions=[AgentTaskActionOut.model_validate(a)
                 for a in task.actions.select_related("agent", "task")],
    )


@router.patch("/{slug}/tasks/{ref}/", response=AgentTaskOut, summary="Update a task's fields",)
def patch_task(request: HttpRequest, slug: str, ref: str, payload: AgentTaskPatch) -> AgentTaskOut:
    agent = agent_for_work(request, slug)
    task = _get_task_or_404(agent, ref)
    data = payload.model_dump(exclude_unset=True)
    try:
        return AgentTaskOut.model_validate(services.patch_task(task, data))
    except (services.UnknownPersonError, ValueError) as exc:
        # TaskTreeError, and a quote whose person_id is not known here.
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
    from apps.contacts import services as contacts

    from . import engagement, participants

    kind = _ENGAGEMENT_OF_ACTION.get(action)
    if kind is None:
        return
    engagement.record(kind, workspace=task.agent.workspace_id, surface=engagement.surface_of(request),
                      user=request.user, project=task.project, task=task)
    if action == "reply":
        try:
            person = contacts.person_for(user=request.user)
            participants.ensure(task.project, person, source="reply")
        except Exception:  # noqa: BLE001 — a participant is never worth a failed reply
            logger.exception("could not add %s as a participant of %s", request.user.pk, task.project_id)


@router.post("/{slug}/tasks/{ref}/actions", response=ActOut,
             summary="Act on a task: approve, decline, reply, nudge or done",)
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
    task = _get_task_or_404(agent, ref)
    if payload.action in _EDITOR_ACTIONS:
        from . import ideas

        # The person whose idea it is may call it shipped, as its owner may (hal T82).
        if not (payload.action == "done" and ideas.is_ideas_person(task, request.user)):
            agent_for_work(request, slug)
    try:
        task, row, turns = services.act(
            task, action=payload.action, comment=payload.comment,
            by=request.user.email or request.user.get_username(), by_user=request.user,
            actor_workspace_ids=visible_agent_workspaces(request),
            # Who may start a turn is the QuickTurn gate (`enqueue_turn` needs
            # write); it decides whether a reply wakes the agent. Never the agent
            # replying on its own card: its note would wake itself, and a turn
            # that replies again would wake it again.
            may_start_turns=(perms.can(request.user, agent.workspace_id, perms.AGENT_WORK)
                             and not agent.is_own_login(request.user)),
            # An ANSWER to an open question wakes the agent whoever writes it (it
            # is the response the agent asked for) — but never the agent itself.
            by_agent_itself=agent.is_own_login(request.user))
    except services.ClosedAskError as exc:
        raise HttpError(409, str(exc)) from exc
    except PermissionError as exc:
        # A proposed project's decision card, by someone who may not decide it.
        raise HttpError(403, str(exc)) from exc
    except ValueError as exc:
        # An empty reply, or an `on_approve` spec that cannot run. `act` is
        # atomic: the ask is still open and the action can be retried.
        raise HttpError(422, str(exc)) from exc
    if task.project_id is not None and not agent.is_own_login(request.user):
        _engaged_with_project_task(request, task, payload.action)
    return ActOut(task=AgentTaskOut.model_validate(task),
                  action=AgentTaskActionOut.model_validate(row),
                  turn_ids=[t.id for t in turns])


# ---- the agent's queue: actions it still has to carry out ----


@router.get("/{slug}/actions/", response=list[AgentTaskActionOut],
            summary="List actions on the agent's tasks (the agent drains ?status=pending)",)
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
