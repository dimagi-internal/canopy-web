"""Fleet-wide task and project lists — `/api/tasks/` and `/api/projects/`.

The same filters as the per-agent routes, across every agent the caller can
see. "Waiting on you" is `GET /api/tasks/?waiting=me`.
"""
from __future__ import annotations

from django.db.models import Case, Count, IntegerField, Q, When
from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.pagination import DEFAULT_LIMIT_CAP, clamp_limit

from . import services
from .api import _visible_agent_workspace_ids
from .models import AgentProject, AgentTask
from .schemas import AgentProjectOut, AgentTaskOut

router = Router(auth=session_auth, tags=["tasks"])

#: A decision to make (review) outranks a question the agent is blocked on,
#: which outranks plain work.
_ASK_RANK = Case(When(ask_kind=AgentTask.ASK_REVIEW, then=0),
                 When(ask_kind=AgentTask.ASK_QUESTION, then=1),
                 default=2, output_field=IntegerField())


@router.get("/tasks/", response=list[AgentTaskOut],
            summary="Tasks across every agent you can see (the per-agent filters, plus agent)")
def list_fleet_tasks(request: HttpRequest, agent: str = "", project: str = "", status: str = "",
                     waiting: str = "", ask: str = "", batch: str = "",
                     limit: int = DEFAULT_LIMIT_CAP) -> list[AgentTaskOut]:
    """Reviews first, then questions, then the rest; oldest first within each.
    `waiting=me` is the caller's inbox: open asks nobody owns plus tasks parked
    on the caller. `limit` caps the rows (at most 500)."""
    # Scope FIRST: `filter_tasks(waiting="me")` adds unrouted open asks from
    # whatever queryset it is handed, so an unscoped one would leak other
    # tenants' asks into the inbox.
    qs = AgentTask.objects.filter(agent__workspace_id__in=_visible_agent_workspace_ids(request))
    if agent:
        qs = qs.filter(agent__slug=agent)
    qs = services.filter_tasks(qs.select_related("agent", "project", "waiting_on_user"),
                               user=request.user, project=project, status=status,
                               waiting=waiting, ask=ask, batch=batch)
    qs = qs.order_by(_ASK_RANK, "created_at", "id")[:clamp_limit(limit)]
    return [AgentTaskOut.model_validate(t) for t in qs]


@router.get("/projects/", response=list[AgentProjectOut],
            summary="Projects across every agent you can see")
def list_fleet_projects(request: HttpRequest, status: str = "active",
                        repo_slug: str = "") -> list[AgentProjectOut]:
    """Active by default; `status=` (empty for all) and `repo_slug=` narrow it."""
    qs = AgentProject.objects.filter(agent__workspace_id__in=_visible_agent_workspace_ids(request))
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
