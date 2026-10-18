"""Fleet-wide task and project lists — `/api/tasks/` and `/api/projects/` — and
workspace projects, addressed `/api/workspace-projects/<workspace>/<handle>/`.

The same filters as the per-agent routes, across every agent the caller can
see. "Waiting on you" is `GET /api/tasks/?waiting=me`.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Case, Count, IntegerField, Q, When
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.pagination import DEFAULT_LIMIT_CAP, clamp_limit

from . import services
from .api import _agent_for_write, _listed_agent_workspace_ids, apply_project_patch, project_detail
from .models import AgentProject, AgentTask
from .schemas import (AgentProjectDetailOut, AgentProjectOut, AgentProjectPatch, AgentTaskOut,
                      WorkspaceProjectIn)

router = Router(auth=session_auth, tags=["tasks"])

#: A decision to make (review) outranks a question the agent is blocked on,
#: which outranks plain work.
_ASK_RANK = Case(When(ask_kind=AgentTask.ASK_REVIEW, then=0),
                 When(ask_kind=AgentTask.ASK_QUESTION, then=1),
                 default=2, output_field=IntegerField())


@router.get("/tasks/", response=list[AgentTaskOut],
            summary="Tasks across every agent you can see (the per-agent filters, plus agent)")
def list_fleet_tasks(request: HttpRequest, agent: str = "", project: str = "", status: str = "",
                     waiting: str = "", ask: str = "", batch: str = "", parent: str = "",
                     limit: int = DEFAULT_LIMIT_CAP) -> list[AgentTaskOut]:
    """Reviews first, then questions, then the rest; oldest first within each.
    `waiting=me` is the caller's inbox: open asks nobody owns plus tasks parked
    on the caller. `limit` caps the rows (at most 500)."""
    # Scope FIRST: `filter_tasks(waiting="me")` adds unrouted open asks from
    # whatever queryset it is handed, so an unscoped one would leak other
    # tenants' asks into the inbox.
    qs = AgentTask.objects.filter(agent__workspace_id__in=_listed_agent_workspace_ids(request))
    if agent:
        qs = qs.filter(agent__slug=agent)
    qs = services.filter_tasks(qs.select_related("agent", "project", "waiting_on_user", "parent")
                               .prefetch_related("human_whys"),
                               user=request.user, project=project, status=status,
                               waiting=waiting, ask=ask, batch=batch, parent=parent)
    qs = qs.order_by(_ASK_RANK, "created_at", "id")[:clamp_limit(limit)]
    return [AgentTaskOut.model_validate(t) for t in qs]


@router.get("/projects/", response=list[AgentProjectOut],
            summary="Projects across every agent you can see")
def list_fleet_projects(request: HttpRequest, status: str = "active",
                        repo_slug: str = "", scope: str = "") -> list[AgentProjectOut]:
    """Active by default; `status=` (empty for all), `repo_slug=` and `scope=`
    (`agent` or `workspace`) narrow it."""
    qs = AgentProject.objects.filter(agent__workspace_id__in=_listed_agent_workspace_ids(request))
    if scope:
        qs = qs.filter(scope=scope)
    if status:
        qs = qs.filter(status=status)
    if repo_slug:
        qs = qs.filter(repo_slug=repo_slug)
    # Counted in the same query: the property would cost two queries a project.
    qs = qs.annotate(
        n_tasks=Count("tasks", distinct=True),
        n_open=Count("tasks", filter=Q(tasks__status__in=services.LIVE_STATUSES), distinct=True),
        n_waiting=Count("tasks", filter=services.waiting_q("tasks__"), distinct=True),
    )
    projects = list(qs.select_related("agent", "owner_user").order_by("agent__slug", "-updated_at"))
    for p in projects:
        p._task_count, p._open_task_count, p._waiting_task_count = p.n_tasks, p.n_open, p.n_waiting
    return [AgentProjectOut.model_validate(p) for p in projects]


# ---- workspace projects: owned by a workspace, kept current by a steward ----


def _workspace_project_or_404(request: HttpRequest, workspace: str, handle: str) -> AgentProject:
    # Same 404 for "no such project" and "not your workspace": no oracle over names.
    if workspace not in set(_listed_agent_workspace_ids(request)):
        raise HttpError(404, f"workspace project {workspace}/{handle} not found")
    project = services.get_workspace_project(workspace, handle)
    if project is None:
        raise HttpError(404, f"workspace project {workspace}/{handle} not found")
    return project


@router.get("/workspace-projects/", response=list[AgentProjectOut],
            summary="Workspace projects in every workspace you can see")
def list_workspace_projects(request: HttpRequest, status: str = "") -> list[AgentProjectOut]:
    """Owned by a workspace rather than one agent, each with a steward agent that
    keeps it current. `status=` narrows; default all."""
    projects = services.workspace_projects(_listed_agent_workspace_ids(request), status=status)
    counts = {r["project_id"]: r for r in AgentTask.objects.filter(project__in=projects)
              .values("project_id").annotate(n=Count("id"),
                                             live=Count("id", filter=Q(status__in=services.LIVE_STATUSES)),
                                             waiting=Count("id", filter=services.waiting_q()))}
    for p in projects:
        c = counts.get(p.pk, {})
        p._task_count, p._open_task_count, p._waiting_task_count = c.get("n", 0), c.get("live", 0), c.get("waiting", 0)
    return [AgentProjectOut.model_validate(p) for p in projects]


@router.post("/workspace-projects/", response={201: AgentProjectOut},
             summary="Create a workspace project, owned by the steward's workspace")
def create_workspace_project(request: HttpRequest, payload: WorkspaceProjectIn):
    steward = _agent_for_write(request, payload.steward.strip())
    try:
        with transaction.atomic():
            project = services.create_project(steward, payload)
            project = services.promote_project(project, payload.handle)
    except services.UnknownPersonError as exc:
        raise HttpError(422, str(exc)) from exc
    except services.ProjectScopeError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    return Status(201, AgentProjectOut.model_validate(project))


@router.get("/workspace-projects/{workspace}/{handle}/", response=AgentProjectDetailOut,
            summary="Get a workspace project: fields, every agent's tasks in it, recent turns")
def get_workspace_project(request: HttpRequest, workspace: str, handle: str) -> AgentProjectDetailOut:
    return project_detail(request, _workspace_project_or_404(request, workspace, handle))


@router.patch("/workspace-projects/{workspace}/{handle}/", response=AgentProjectOut,
              summary="Update a workspace project (including handing it to another steward)")
def patch_workspace_project(request: HttpRequest, workspace: str, handle: str,
                            payload: AgentProjectPatch) -> AgentProjectOut:
    project = _workspace_project_or_404(request, workspace, handle)
    _agent_for_write(request, project.agent.slug)  # the steward's write gate
    data = payload.model_dump(exclude_unset=True)
    if "links" in data and data["links"] is not None:
        data["links"] = [link if isinstance(link, dict) else link.model_dump() for link in data["links"]]
    return AgentProjectOut.model_validate(apply_project_patch(request, project, data))
