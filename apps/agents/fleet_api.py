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
from apps.api.pagination import DEFAULT_LIMIT_CAP, Page, clamp_limit, paginate
from apps.harness.api_turns import visible_turns_qs
from apps.harness.models import Turn
from apps.tokens.views_mcp_oauth import issuer
from apps.workspaces import services as wsvc
from apps.workspaces.links import scoped_path
from apps.workspaces.models import Workspace

from . import kb_store, kb_vectors, services
from .api import list_agents
from .api_board import apply_project_patch, project_detail, require_workspace_editor
from .gates import agent_for_work, listed_agent_workspaces, visible_agent_workspaces
from .health import _counts
from .models import Agent, AgentProject, AgentTask
from .schemas import (
    AgentOut,
    AgentProjectDetailOut,
    AgentProjectOut,
    AgentProjectPatch,
    AgentRosterRowOut,
    AgentTaskOut,
    KbOriginalsIn,
    KbOriginalsOut,
    KbPublishIn,
    KbUploadOut,
    McpServerOut,
    WorkspaceProjectIn,
)

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
    qs = AgentTask.objects.filter(agent__workspace_id__in=listed_agent_workspaces(request))
    if agent:
        qs = qs.filter(agent__slug=agent)
    qs = services.filter_tasks(qs.select_related("agent", "project", "waiting_on_user", "parent")
                               .prefetch_related("human_whys"),
                               user=request.user, project=project, status=status,
                               waiting=waiting, ask=ask, batch=batch, parent=parent)
    qs = qs.order_by(_ASK_RANK, "created_at", "id")[:clamp_limit(limit)]
    return [AgentTaskOut.model_validate(t) for t in qs]


@router.get("/me/tasks/", response=list[AgentTaskOut],
            summary="Tasks across every agent you can see, in every workspace you are in")
def list_my_tasks(request: HttpRequest, agent: str = "", project: str = "", status: str = "",
                  waiting: str = "", ask: str = "", limit: int = DEFAULT_LIMIT_CAP) -> list[AgentTaskOut]:
    """The fleet task list as a user-level view: each organisation is read on
    its own (`each_organisation`). `waiting=me` is your inbox everywhere. Same
    order: reviews, then questions, then the rest; oldest first within each."""
    rows = wsvc.each_organisation(request, lambda: list_fleet_tasks(
        request, agent=agent, project=project, status=status, waiting=waiting, ask=ask, limit=limit))
    rows.sort(key=lambda t: ({AgentTask.ASK_REVIEW: 0, AgentTask.ASK_QUESTION: 1}.get(t.ask_kind, 2), t.created_at))
    return rows[:clamp_limit(limit)]


@router.get("/me/agents/", response=Page[AgentOut], summary="Every agent you can see, in every workspace you are in")
def list_my_agents(request: HttpRequest, limit: int = 100) -> Page[AgentOut]:
    """The agent list as a user-level view: each organisation read on its own,
    every row carrying its workspace. By name."""
    rows = wsvc.each_organisation(request, lambda: list_agents(request, limit=limit).items)
    return paginate(sorted(rows, key=lambda a: a.name.lower()), offset=0, limit=clamp_limit(limit))


@router.get("/projects/", response=list[AgentProjectOut],
            summary="Projects across every agent you can see")
def list_fleet_projects(request: HttpRequest, status: str = "active",
                        repo_slug: str = "", scope: str = "") -> list[AgentProjectOut]:
    """Active by default; `status=` (empty for all), `repo_slug=` and `scope=`
    (`agent` or `workspace`) narrow it."""
    qs = AgentProject.objects.filter(agent__workspace_id__in=listed_agent_workspaces(request))
    if scope:
        qs = qs.filter(scope=scope)
    if repo_slug:
        qs = qs.filter(repo_slug=repo_slug)
    return _project_rows(qs, status=status)


@router.get("/me/projects/", response=list[AgentProjectOut],
            summary="The projects you own, across every workspace you are in")
def list_my_projects(request: HttpRequest, status: str = "active") -> list[AgentProjectOut]:
    """A user-level view: each organisation is read on its own
    (`each_organisation`) and every row carries its workspace. Active by
    default; `status=` (empty for all)."""
    return wsvc.each_organisation(request, lambda: _project_rows(
        AgentProject.objects.filter(agent__workspace_id__in=listed_agent_workspaces(request),
                                    owner_user=request.user),
        status=status,
    ))


def _project_rows(qs, *, status: str) -> list[AgentProjectOut]:
    if status:
        qs = qs.filter(status=status)
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


@router.get("/agent-roster/", response=list[AgentRosterRowOut],
            summary="Every agent you can see, with what it is doing")
def agent_roster(request: HttpRequest) -> list[AgentRosterRowOut]:
    """One row per agent: open tasks and those waiting on a person, active
    projects, skills, schedules, and its turns — the last one and how it ended,
    and how many ran and failed in the last 7 days."""
    import datetime as dt

    from django.db.models import Max
    from django.utils import timezone

    visible = listed_agent_workspaces(request)
    agents = list(Agent.objects.filter(workspace_id__in=visible).select_related("owner").order_by("name"))
    counts = _counts(agents)
    since = timezone.now() - dt.timedelta(days=7)
    failed = [Turn.FAILED, Turn.LOST]
    turns = visible_turns_qs(request).filter(agent__in=agents).order_by()
    stats = {r["agent_id"]: r for r in turns.values("agent_id").annotate(
        last=Max("created_at"),
        n7=Count("id", filter=Q(created_at__gte=since)),
        f7=Count("id", filter=Q(created_at__gte=since, status__in=failed)))}
    last_status = {(r["agent_id"], r["created_at"]): r["status"] for r in turns.filter(
        created_at__in=[r["last"] for r in stats.values()]).values("agent_id", "created_at", "status")}
    rows = []
    for a in agents:
        c, t = counts.get(a.pk, {}), stats.get(a.pk, {})
        rows.append(AgentRosterRowOut(
            slug=a.slug, name=a.name, description=a.description, workspace=a.workspace_id,
            avatar_url=a.avatar_url, email=a.email, turn_mode=a.turn_mode,
            owner_email=a.owner.email if a.owner_id else None,
            url=scoped_path(a.workspace_id, f"/agents/{a.slug}"),
            skills=c.get("skills", 0), schedules=c.get("schedules", 0),
            active_projects=c.get("projects", 0), open_tasks=c.get("open_tasks", 0),
            waiting_tasks=c.get("waiting_tasks", 0),
            last_turn_at=t.get("last"), last_turn_status=last_status.get((a.pk, t.get("last")), ""),
            turns_7d=t.get("n7", 0), failed_7d=t.get("f7", 0),
        ))
    return rows


# ---- workspace projects: owned by a workspace, kept current by a steward ----


def _workspace_project_or_404(request: HttpRequest, workspace: str, handle: str) -> AgentProject:
    # Same 404 for "no such project" and "not your workspace": no oracle over names.
    # Readable from its workspace, and from its steward's (a dimagi agent keeping
    # connect's project reads it as the dimagi member it is).
    project = services.get_workspace_project(workspace, handle)
    if project is None or (workspace not in set(listed_agent_workspaces(request))
                           and project.agent.workspace_id not in visible_agent_workspaces(request)):
        raise HttpError(404, f"workspace project {workspace}/{handle} not found")
    return project


@router.get("/workspace-projects/", response=list[AgentProjectOut],
            summary="Workspace projects in every workspace you can see")
def list_workspace_projects(request: HttpRequest, status: str = "") -> list[AgentProjectOut]:
    """Owned by a workspace rather than one agent, each with a steward agent that
    keeps it current. `status=` narrows; default all."""
    listed = listed_agent_workspaces(request)
    projects = services.workspace_projects(listed, status=status, steward_workspaces=listed)
    counts = {r["project_id"]: r for r in AgentTask.objects.filter(project__in=projects)
              .values("project_id").annotate(n=Count("id"),
                                             live=Count("id", filter=Q(status__in=services.LIVE_STATUSES)),
                                             waiting=Count("id", filter=services.waiting_q()))}
    for p in projects:
        c = counts.get(p.pk, {})
        p._task_count, p._open_task_count, p._waiting_task_count = c.get("n", 0), c.get("live", 0), c.get("waiting", 0)
    return [AgentProjectOut.model_validate(p) for p in projects]


@router.get("/mcp-servers/", response=list[McpServerOut],
            summary="The MCP servers you can add: workspace projects serving their own tools")
def list_mcp_servers(request: HttpRequest) -> list[McpServerOut]:
    """A directory to browse, not a subscription: nothing here is added to anyone's
    sessions. Pinned to a workspace (`/api/w/<ws>/…`, or the tool's `workspace`), it
    covers that workspace and the ones below it you belong to. Every row is one you
    can call: the readers a project's server admits (its workspace's members and
    its steward's) are exactly the ones a list spanning those workspaces reaches."""

    listed = listed_agent_workspaces(request)
    projects = [p for p in services.workspace_projects(listed, steward_workspaces=listed)
                if p.mcp_path and p.status != AgentProject.ARCHIVED]
    names = dict(Workspace.objects.filter(slug__in={p.owner_workspace_id for p in projects})
                 .values_list("slug", "display_name"))
    base = issuer(request)
    out = []
    for p in projects:
        url = f"{base}{p.mcp_path}".rstrip("/")
        out.append(McpServerOut(
            name=p.handle, address=f"{p.owner_workspace_id}/{p.handle}", workspace=p.owner_workspace_id,
            workspace_name=names.get(p.owner_workspace_id, p.owner_workspace_id), title=p.name,
            description=p.outcome, kind=p.mcp_kind, steward_slug=p.agent.slug, url=url,
            add_command=f"claude mcp add --transport http {p.handle} {url}",
            project_url=f"/w/{p.owner_workspace_id}/projects/{p.handle}",
            published_at=p.kb_published_at, bytes=p.kb_bytes))
    return out


@router.post("/workspace-projects/", response={201: AgentProjectOut},
             summary="Create a workspace project, owned by the steward's workspace")
def create_workspace_project(request: HttpRequest, payload: WorkspaceProjectIn):
    steward = agent_for_work(request, payload.steward.strip())
    require_workspace_editor(request, payload.workspace.strip(), steward)
    try:
        with transaction.atomic():
            project = services.create_project(steward, payload)
            project = services.promote_project(project, payload.handle, payload.workspace)
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
    agent_for_work(request, project.agent.slug)  # the steward's write gate
    data = payload.model_dump(exclude_unset=True)
    if "links" in data and data["links"] is not None:
        data["links"] = [link if isinstance(link, dict) else link.model_dump() for link in data["links"]]
    return AgentProjectOut.model_validate(apply_project_patch(request, project, data))


# ---- its own MCP server: the steward publishes a knowledge base it serves ----


def _kb_call(fn):

    try:
        return fn()
    except kb_store.KbNotConfigured as exc:
        raise HttpError(503, str(exc)) from exc
    except kb_store.KbPublishError as exc:
        raise HttpError(422, str(exc)) from exc


@router.post("/workspace-projects/{workspace}/{handle}/kb/upload", response=KbUploadOut,
             summary="Get a presigned upload for a new knowledge base (the steward's curator)")
def kb_upload_target(request: HttpRequest, workspace: str, handle: str) -> KbUploadOut:
    """Step one of publishing: PUT the gzipped kb.sqlite to `url` with `headers`,
    then `POST …/kb/publish`. The bytes never pass through canopy."""

    project = _workspace_project_or_404(request, workspace, handle)
    agent_for_work(request, project.agent.slug)
    return KbUploadOut(**_kb_call(lambda: kb_store.upload_target(project)))


@router.post("/workspace-projects/{workspace}/{handle}/kb/originals", response=KbOriginalsOut,
             summary="Get presigned uploads for the original files a knowledge base points at")
def kb_upload_originals(request: HttpRequest, workspace: str, handle: str, payload: KbOriginalsIn) -> KbOriginalsOut:
    """Before publishing a build: name the originals its items point at (sha256, size,
    type); the answer has a PUT for each one the project's store lacks. Stored by
    sha256 and never deleted, so an unchanged file uploads once and a changed one is
    kept beside its earlier versions. The `original` tool hands readers these files."""

    project = _workspace_project_or_404(request, workspace, handle)
    agent_for_work(request, project.agent.slug)
    files = [f.model_dump() for f in payload.files]
    return KbOriginalsOut(**_kb_call(lambda: kb_store.originals_upload(project, files)))


@router.post("/workspace-projects/{workspace}/{handle}/kb/publish", response=AgentProjectOut,
             summary="Publish an uploaded knowledge base: the project then serves it as its MCP server")
def publish_kb(request: HttpRequest, workspace: str, handle: str, payload: KbPublishIn) -> AgentProjectOut:
    """The project answers its tools at `mcp_path` from this upload from now on
    (every serving instance re-fetches it by its sha256); the previous one is deleted."""

    project = _workspace_project_or_404(request, workspace, handle)
    agent_for_work(request, project.agent.slug)
    project = _kb_call(lambda: kb_store.publish(project, payload.key, payload.sha256))
    # Dormant: a no-op while KB_VECTORS is off (kb_vectors.py). When on, it brings
    # the vectors in step with the new file off the request.

    kb_vectors.sync_after_publish(project)
    return AgentProjectOut.model_validate(project)
