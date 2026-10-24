"""The workspace's task and project lists — `/api/tasks/` and `/api/projects/` —
and every project's own address, `/api/workspace-projects/<workspace>/<handle>/`.

The same filters as the per-agent routes, across every workspace the caller
can see. "Waiting on you" is `GET /api/tasks/?waiting=me`; "My tasks" is
`?assignee=me`. One task by key is `tasks_api`.
"""
from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.db.models import Case, Count, IntegerField, Q, When, prefetch_related_objects
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.pagination import DEFAULT_LIMIT_CAP, Page, clamp_limit, paginate
from apps.api.route_gates import gate
from apps.harness.api_turns import visible_turns_qs
from apps.harness.models import Turn
from apps.oauth_server.views import issuer
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.links import scoped_path
from apps.workspaces.models import Workspace

from . import kb_store, kb_vectors, kb_versions, services, work
from .api import list_agents
from .api_board import apply_project_patch, project_detail, require_workspace_editor
from .gates import (
    agent_for_read,
    agent_for_work,
    listed_agent_workspaces,
    project_ws_or_404,
    require_project_edit,
    visible_agent_workspaces,
)
from .health import _counts
from .models import Agent, AgentProject, AgentTask, ProjectService
from .schemas import (
    AgentOut,
    AgentProjectDetailOut,
    AgentProjectOut,
    AgentProjectPatch,
    AgentRosterRowOut,
    AgentTaskOut,
    KbOriginalsIn,
    TaskCreateIn,
    KbOriginalsOut,
    KbPublishIn,
    KbUploadOut,
    McpServerOut,
    TaskCountOut,
    WorkspaceProjectIn,
)

router = Router(auth=session_auth, tags=["tasks"])

#: A decision to make (review) outranks a question the agent is blocked on,
#: which outranks plain work.
_ASK_RANK = Case(When(ask_kind=AgentTask.ASK_REVIEW, then=0),
                 When(ask_kind=AgentTask.ASK_QUESTION, then=1),
                 default=2, output_field=IntegerField())


#: `sort=` values: the column, and its order.
_SORTS = {
    "": None,  # reviews, then questions, then the rest; oldest first in each
    "rank": ("rank", "id"),
    "created": ("-created_at", "-id"),
    "updated": ("-updated_at", "-id"),
    "priority": (Case(When(priority=0, then=9), default="priority", output_field=IntegerField()), "rank"),
    "due": ("due", "rank"),
    "status": (Case(*[When(status=st, then=i) for i, st in enumerate(AgentTask.STATUS_ORDER)],
                    output_field=IntegerField()), "rank"),
    "title": ("title", "id"),
}


def task_query(request: HttpRequest, *, agent: str = "", project: str = "", status: str = "",
               waiting: str = "", ask: str = "", batch: str = "", parent: str = "", assignee: str = "",
               created_by: str = "", label: str = "", priority: str = "", milestone: str = "",
               category: str = "", kind: str = "", due_before=None, due_after=None, q: str = "",
               snoozed: str = ""):
    """The workspace task list's rows, scoped to what the caller may list."""
    # Scope FIRST: `filter_tasks(waiting="me")` adds unrouted open asks from
    # whatever queryset it is handed, so an unscoped one would leak other
    # tenants' asks into the inbox.
    qs = AgentTask.objects.filter(workspace_id__in=listed_agent_workspaces(request))
    if agent:
        qs = qs.filter(Q(agent__slug=agent) | Q(created_by_agent__slug=agent, agent__isnull=True))
    return services.filter_tasks(work.task_rows(qs), user=request.user, project=project, status=status,
                                 waiting=waiting, ask=ask, batch=batch, parent=parent, assignee=assignee,
                                 created_by=created_by, label=label, priority=priority,
                                 milestone=milestone, category=category, kind=kind,
                                 due_before=due_before, due_after=due_after, q=q, snoozed=snoozed)


@router.get("/tasks/", response=list[AgentTaskOut],
            summary="Tasks across every workspace you can see, filtered")
@gate("member")
def list_fleet_tasks(request: HttpRequest, agent: str = "", project: str = "", status: str = "",
                     waiting: str = "", ask: str = "", batch: str = "", parent: str = "",
                     assignee: str = "", created_by: str = "", label: str = "", priority: str = "",
                     milestone: str = "", category: str = "", kind: str = "",
                     due_before: dt.date | None = None, due_after: dt.date | None = None,
                     q: str = "", snoozed: str = "", sort: str = "",
                     limit: int = DEFAULT_LIMIT_CAP) -> list[AgentTaskOut]:
    """`waiting=me` is the caller's inbox: open asks nobody owns plus tasks
    parked on the caller. `assignee` / `created_by`: `me`, an email,
    `agent:<slug>` or `none`. `agent`: the tasks that agent's board shows.
    `category`: triage, backlog, unstarted, started, completed, canceled
    (comma list). `label`, `priority` (0-4), `status`: comma lists. `q`
    matches a title, key or next action. `snoozed=hide` leaves out tasks
    snoozed into the future. `sort`: rank, created, updated, priority, due,
    status, title — default reviews first, then questions, then the rest,
    oldest first in each. `limit` caps the rows (at most 500)."""
    qs = task_query(request, agent=agent, project=project, status=status, waiting=waiting, ask=ask,
                    batch=batch, parent=parent, assignee=assignee, created_by=created_by, label=label,
                    priority=priority, milestone=milestone, category=category, kind=kind,
                    due_before=due_before, due_after=due_after, q=q, snoozed=snoozed)
    order = _SORTS.get(sort)
    if sort not in _SORTS:
        raise HttpError(422, f"sort must be one of {', '.join(k for k in _SORTS if k)}")
    qs = qs.order_by(*order) if order else qs.order_by(_ASK_RANK, "created_at", "id")
    return [AgentTaskOut.model_validate(t) for t in qs[:clamp_limit(limit)]]


@router.post("/tasks/", response={201: AgentTaskOut}, summary="Create a task in a workspace")
@gate("agent.work", kind="content")
def create_task(request: HttpRequest, payload: TaskCreateIn) -> Status:
    """Filed by you, in `workspace` (default: its project's, else the one you
    are pinned to, else your only one), assigned as `assignee` says (nobody
    when omitted). `project` is an address (`<ws>/<handle>`) or a handle there."""
    data = payload.model_dump(exclude_unset=True)
    ws = (data.pop("workspace", "") or "").strip() or getattr(request, "workspace_slug", None) or ""
    ref = (data.get("project") or "").strip()
    if ref and "/" in ref:
        ws = ws or ref.split("/", 1)[0]
    if not ws:
        found = wsvc.creation_workspace(request)
        if found is None:
            raise HttpError(422, wsvc.creation_refusal(request))
        ws = found.slug
    if ws not in visible_agent_workspaces(request):
        raise HttpError(404, f"workspace '{ws}' not found")
    if not perms.can(request.user, ws, perms.AGENT_WORK):
        raise HttpError(403, "creating a task requires the editor role")
    if data.get("on_approve"):
        raise HttpError(422, "on_approve is set by an agent filing its own work (its route), not here")
    if ref and services._project_ref(None, ref, ws) is None:
        raise HttpError(422, f"no project {ref!r} in {ws}")
    if (data.get("assignee") or "").strip() and "@" not in data["assignee"]:
        agent_for_read(request, data["assignee"].removeprefix("agent:"), honor_pin=False)
    try:
        [task] = services.create_tasks(None, [data], workspace=ws, by_user=request.user)
    except services.UnknownPersonError as exc:
        raise HttpError(422, str(exc)) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, AgentTaskOut.model_validate(task))


@router.get("/tasks/count/", response=TaskCountOut,
            summary="How many tasks match the task list's filters (the badge's number)")
@gate("member")
def count_tasks(request: HttpRequest, agent: str = "", project: str = "", status: str = "",
                waiting: str = "", ask: str = "", assignee: str = "", created_by: str = "",
                label: str = "", priority: str = "", category: str = "", kind: str = "",
                snoozed: str = "", parent: str = "") -> TaskCountOut:
    """The same filters as `GET /api/tasks/`, answered as one number, so a badge
    never downloads the list it counts."""
    qs = task_query(request, agent=agent, project=project, status=status, waiting=waiting, ask=ask,
                    assignee=assignee, created_by=created_by, label=label, priority=priority,
                    category=category, kind=kind, snoozed=snoozed, parent=parent)
    return TaskCountOut(count=qs.order_by().values("pk").distinct().count())


@router.get("/me/tasks/", response=list[AgentTaskOut],
            summary="Tasks across every agent you can see, in every workspace you are in")
@gate("member")  # list_fleet_tasks per organisation
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
@gate("member")  # list_agents per organisation
def list_my_agents(request: HttpRequest, limit: int = 100) -> Page[AgentOut]:
    """The agent list as a user-level view: each organisation read on its own,
    every row carrying its workspace. By name."""
    rows = wsvc.each_organisation(request, lambda: list_agents(request, limit=limit).items)
    return paginate(sorted(rows, key=lambda a: a.name.lower()), offset=0, limit=clamp_limit(limit))


@router.get("/projects/", response=list[AgentProjectOut],
            summary="Projects across every agent you can see")
@gate("member")
def list_fleet_projects(request: HttpRequest, status: str = "active",
                        repo_slug: str = "", scope: str = "") -> list[AgentProjectOut]:
    """Active by default; `status=` (a comma list; empty for all, `open` for
    every status still open) and `repo_slug=` narrow it."""
    qs = AgentProject.objects.filter(workspace_id__in=listed_agent_workspaces(request))
    # `scope` is accepted and ignored: every project is the workspace's now.
    if repo_slug:
        qs = qs.filter(repo_slug=repo_slug)
    return _project_rows(qs, status=status)


@router.get("/me/projects/", response=list[AgentProjectOut],
            summary="The projects you own, across every workspace you are in")
# User-level views (`/api/me/…`): the same list once per organisation
# (`wsvc.each_organisation`), each run under that list's own gate.
# the caller's own projects
@gate("self")
def list_my_projects(request: HttpRequest, status: str = "active") -> list[AgentProjectOut]:
    """A user-level view: each organisation is read on its own
    (`each_organisation`) and every row carries its workspace. Active by
    default; `status=` (empty for all)."""
    return wsvc.each_organisation(request, lambda: _project_rows(
        AgentProject.objects.filter(workspace_id__in=listed_agent_workspaces(request),
                                    owner_user=request.user),
        status=status,
    ))


def _project_rows(qs, *, status: str) -> list[AgentProjectOut]:
    if status == "open":
        qs = qs.filter(status__in=AgentProject.OPEN_STATUSES)
    elif status:
        qs = qs.filter(status__in=[st.strip() for st in status.split(",") if st.strip()])
    projects = list(qs.select_related("agent", "owner_user").order_by("workspace_id", "-updated_at"))
    work.annotate_projects(projects)
    return [AgentProjectOut.model_validate(p) for p in projects]


@router.get("/agent-roster/", response=list[AgentRosterRowOut],
            summary="Every agent you can see, with what it is doing")
@gate("member")  # counts over the same agents list_agents shows; turns through visible_turns_qs
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
    return project_ws_or_404(request, workspace, handle)


@router.get("/workspace-projects/", response=list[AgentProjectOut],
            summary="Workspace projects in every workspace you can see")
@gate("member")
def list_workspace_projects(request: HttpRequest, status: str = "") -> list[AgentProjectOut]:
    """Every project of those workspaces (and those led from them), each
    addressed `<workspace>/<handle>`. `status=` (a comma list) narrows; default
    all."""
    listed = listed_agent_workspaces(request)
    projects = services.workspace_projects(listed, status=status, steward_workspaces=listed)
    work.annotate_projects(projects)
    return [AgentProjectOut.model_validate(p) for p in projects]


@router.get("/mcp-servers/", response=list[McpServerOut],
            summary="The MCP servers you can add: what the workspace's projects serve and share")
@gate("member")
def list_mcp_servers(request: HttpRequest) -> list[McpServerOut]:
    """A directory to browse, not a subscription: nothing here is added to anyone's
    sessions. Two kinds of row: a project's own knowledge-base server (`kind: kb`,
    served by canopy), and a remote MCP server a project shares with the workspace
    (`kind: mcp`, its URL as the project declared it — canopy neither proxies nor
    vets it). Pinned to a workspace (`/api/w/<ws>/…`, or the tool's `workspace`), it
    covers that workspace and the ones below it you belong to. Every row is one you
    can call: the readers a project's server admits (its workspace's members and
    its lead agent's) are exactly the ones a list spanning those workspaces reaches."""

    listed = listed_agent_workspaces(request)
    projects = [p for p in services.workspace_projects(listed, steward_workspaces=listed)
                if p.status != AgentProject.ARCHIVED]
    prefetch_related_objects(projects, "served")
    names = dict(Workspace.objects.filter(slug__in={p.workspace_id for p in projects})
                 .values_list("slug", "display_name"))
    base = issuer(request)
    out = []
    for p in projects:
        common = {"address": p.address, "workspace": p.workspace_id,
                  "workspace_name": names.get(p.workspace_id, p.workspace_id),
                  "steward_slug": p.agent_slug or "", "project_url": p.url}
        if p.mcp_path:
            url = f"{base}{p.mcp_path}".rstrip("/")
            out.append(McpServerOut(
                name=p.handle, title=p.name, description=p.outcome, kind=p.mcp_kind, url=url,
                add_command=f"claude mcp add --transport http {p.handle} {url}",
                published_at=p.kb_published_at, bytes=p.knowledge_base.kb_bytes, **common))
        for svc in p.served.all():
            if svc.kind != ProjectService.MCP:
                continue
            out.append(McpServerOut(
                name=svc.name, title=f"{svc.name} · {p.name}", description=svc.description, kind=svc.kind,
                url=svc.url, add_command=f"claude mcp add --transport http {svc.name} {svc.url}",
                published_at=svc.updated_at, bytes=None, **common))
    return out


@router.post("/workspace-projects/", response={201: AgentProjectOut},
             summary="Create a project in a workspace, optionally led by an agent")
@gate("agent.work", kind="content")  # an editor of the workspace (and of the lead agent's)
def create_workspace_project(request: HttpRequest, payload: WorkspaceProjectIn):
    """In `workspace` — default the lead agent's, else the one you are pinned to
    or the only one you are in — led by `steward` (an agent's slug) when given,
    with `member_agents` working on it. The handle is free from its name when
    omitted."""
    steward = agent_for_work(request, payload.steward.strip()) if payload.steward.strip() else None
    workspace = payload.workspace.strip() or (steward.workspace_id if steward else "")
    if not workspace:
        ws = wsvc.creation_workspace(request)
        if ws is None:
            raise HttpError(422, wsvc.creation_refusal(request))
        workspace = ws.slug
    require_workspace_editor(request, workspace, steward)
    if steward is None and not perms.can(request.user, workspace, perms.AGENT_WORK):
        raise HttpError(403, "creating a project requires the editor role")
    try:
        with transaction.atomic():
            project = services.create_project(steward, payload, workspace=workspace, by_user=request.user)
            if payload.member_agents:
                services.set_member_agents(project, [agent_for_read(request, m.strip(), honor_pin=False)
                                                     for m in payload.member_agents if m.strip()])
    except services.UnknownPersonError as exc:
        raise HttpError(422, str(exc)) from exc
    except services.ProjectScopeError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    return Status(201, AgentProjectOut.model_validate(project))


@router.get("/workspace-projects/{workspace}/{handle}/", response=AgentProjectDetailOut,
            summary="Get a project: fields, its tasks, progress, recent turns")
@gate("member")
def get_workspace_project(request: HttpRequest, workspace: str, handle: str) -> AgentProjectDetailOut:
    return project_detail(request, _workspace_project_or_404(request, workspace, handle))


@router.patch("/workspace-projects/{workspace}/{handle}/", response=AgentProjectOut,
              summary="Update a project (including its lead agent and its agents)")
# an editor of its workspace or its lead agent's, its lead, or a working agent's own login
@gate("agent.work", "agent-login", "self", kind="content")
def patch_workspace_project(request: HttpRequest, workspace: str, handle: str,
                            payload: AgentProjectPatch) -> AgentProjectOut:
    project = _workspace_project_or_404(request, workspace, handle)
    require_project_edit(request, project)
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
@gate("agent.work", kind="content")  # the steward's write gate: its curator publishes
def kb_upload_target(request: HttpRequest, workspace: str, handle: str) -> KbUploadOut:
    """Step one of publishing: PUT the gzipped kb.sqlite to `url` with `headers`,
    then `POST …/kb/publish`. The bytes never pass through canopy."""

    project = _workspace_project_or_404(request, workspace, handle)
    require_project_edit(request, project)
    return KbUploadOut(**_kb_call(lambda: kb_store.upload_target(project)))


@router.post("/workspace-projects/{workspace}/{handle}/kb/originals", response=KbOriginalsOut,
             summary="Get presigned uploads for the original files a knowledge base points at")
@gate("agent.work", kind="content")
def kb_upload_originals(request: HttpRequest, workspace: str, handle: str, payload: KbOriginalsIn) -> KbOriginalsOut:
    """Before publishing a build: name the originals its items point at (sha256, size,
    type); the answer has a PUT for each one the project's store lacks. Stored by
    sha256 and never deleted, so an unchanged file uploads once and a changed one is
    kept beside its earlier versions. The `original` tool hands readers these files."""

    project = _workspace_project_or_404(request, workspace, handle)
    require_project_edit(request, project)
    files = [f.model_dump() for f in payload.files]
    return KbOriginalsOut(**_kb_call(lambda: kb_store.originals_upload(project, files)))


@router.post("/workspace-projects/{workspace}/{handle}/kb/publish", response=AgentProjectOut,
             summary="Publish an uploaded knowledge base: the project then serves it as its MCP server")
@gate("agent.work", kind="content")
def publish_kb(request: HttpRequest, workspace: str, handle: str, payload: KbPublishIn) -> AgentProjectOut:
    """The project answers its tools at `mcp_path` from this upload from now on
    (every serving instance re-fetches it by its sha256), as a new FULL version of its
    log (`…/kb/versions`) made from version `base`: 409 when another landed since. It
    carries the `curation` state it was built from, which the next refresh starts from.
    Earlier versions' files are kept for rollback (the newest few)."""

    project = _workspace_project_or_404(request, workspace, handle)
    require_project_edit(request, project)
    try:
        project, _ = _kb_call(lambda: kb_versions.publish_full(project, payload.key, payload.sha256,
                                                               base=payload.base, curation=payload.curation,
                                                               published_by=kb_versions.publisher(request.user),
                                                               changes=payload.changes))
    except kb_versions.KbConflict as exc:
        raise HttpError(409, str(exc)) from exc
    # Dormant: a no-op while KB_VECTORS is off (kb_vectors.py). When on, it brings
    # the vectors in step with the new file off the request.

    kb_vectors.sync_after_publish(project)
    return AgentProjectOut.model_validate(project)
