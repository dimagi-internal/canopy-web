"""`/api/dynamic-views/`: make, change, version and read dynamic views.

Every route is also an MCP tool, which is how an agent generates a view: read
the guide (`GET /guide/`), then `POST /` the code and its sources. Reading a
view needs membership of its workspace; changing one needs the editor role
there, or being able to edit the view's project. Design:
docs/architecture/dynamic-views.md.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.agents import services as agent_services
from apps.agents.gates import listed_agent_workspaces, may_edit_project, visible_agent_workspaces
from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.links import scoped_path_or_none

from . import catalog, guide, services
from .models import DynamicView
from .schemas import (
    DynamicViewGuideOut,
    DynamicViewIn,
    DynamicViewOut,
    DynamicViewPatch,
    DynamicViewRevisionOut,
    DynamicViewRollbackIn,
    DynamicViewSummaryOut,
)

router = Router(auth=session_auth, tags=["dynamic-views"])


def _summary_fields(view: DynamicView) -> dict:
    return dict(
        id=view.pk, workspace=view.workspace_id,
        project=view.project.handle if view.project_id else None,
        project_name=view.project.name if view.project_id else None,
        slug=view.slug, title=view.title, description=view.description, version=view.version,
        url=scoped_path_or_none(view.workspace_id, f"/views/{view.pk}"),
        updated_by_email=view.updated_by.email if view.updated_by_id else None,
        updated_at=view.updated_at,
    )


def _out(view: DynamicView) -> DynamicViewOut:
    return DynamicViewOut(**_summary_fields(view), code=view.code, sources=view.sources or [],
                          created_by_email=view.created_by.email if view.created_by_id else None,
                          created_at=view.created_at)


def _view_or_404(request: HttpRequest, view_id: int) -> DynamicView:
    view = DynamicView.objects.select_related("project", "created_by", "updated_by").filter(pk=view_id).first()
    if view is None or view.workspace_id not in visible_agent_workspaces(request):
        raise HttpError(404, f"view {view_id} not found")
    return view


def _project_or_422(workspace: str, handle: str):
    project = agent_services.get_workspace_project(workspace, handle)
    if project is None:
        raise HttpError(422, f"no project '{handle}' in workspace '{workspace}'")
    return project


def _require_edit(request: HttpRequest, workspace: str, project=None) -> None:
    if perms.can(request.user, workspace, perms.AGENT_WORK):
        return
    if project is not None and may_edit_project(request, project):
        return
    raise HttpError(403, "changing a view needs the editor role in its workspace, or edit rights on its project")


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


@router.get("/guide/", response=DynamicViewGuideOut,
            summary="How to write a dynamic view: the render contract, the data catalog, an example")
@gate("authenticated")
def get_dynamic_view_guide(request: HttpRequest) -> DynamicViewGuideOut:
    """Read this before generating a view. `catalog` is every read a view may
    ask for; `contract` is the props, the `canopy` API and the component kit the
    sandbox gives the code; `example` is a complete view and `example_sources`
    the sources it reads."""
    return DynamicViewGuideOut(contract=guide.CONTRACT, catalog=catalog.catalog(), example=guide.EXAMPLE,
                               example_sources=guide.EXAMPLE_SOURCES)


@router.get("/", response=list[DynamicViewSummaryOut], summary="The dynamic views of a workspace, or of one project")
@gate("member")
def list_dynamic_views(request: HttpRequest, project: str = "") -> list[DynamicViewSummaryOut]:
    """Every view in the workspaces this request reads (pin one with `/api/w/<ws>/`).
    `project=<handle>` narrows to that project's views."""
    qs = (DynamicView.objects.filter(workspace_id__in=list(listed_agent_workspaces(request)))
          .select_related("project", "updated_by"))
    if project:
        qs = qs.filter(project__handle=project)
    return [DynamicViewSummaryOut(**_summary_fields(v)) for v in qs]


@router.post("/", response={201: DynamicViewOut}, summary="Create a dynamic view")
@gate("agent.work", kind="content")
def create_dynamic_view(request: HttpRequest, payload: DynamicViewIn) -> Status:
    """Validates the code (it must define `View`) and every source against the
    catalog; a refusal names what is wrong. Returns the view with its `url`."""
    ws = _ws_for_write(request, payload.workspace)
    project = _project_or_422(ws, payload.project) if payload.project else None
    _require_edit(request, ws, project)
    try:
        view = services.create(workspace_id=ws, project=project, data=payload.model_dump(), user=request.user)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, _out(view))


@router.get("/{view_id}", response=DynamicViewOut, summary="One dynamic view, with its code and sources")
@gate("member")
def get_dynamic_view(request: HttpRequest, view_id: int) -> DynamicViewOut:
    return _out(_view_or_404(request, view_id))


@router.patch("/{view_id}", response=DynamicViewOut, summary="Change a dynamic view (a new version)")
@gate("agent.work", kind="content")
def update_dynamic_view(request: HttpRequest, view_id: int, payload: DynamicViewPatch) -> DynamicViewOut:
    """Only the fields given change. Each change that alters something is a new
    version; `GET /{id}/revisions/` lists them and `POST /{id}/rollback` restores one."""
    view = _view_or_404(request, view_id)
    _require_edit(request, view.workspace_id, view.project)
    data = payload.model_dump(exclude_unset=True)
    project_given = "project" in data
    project = None
    if project_given and data["project"]:
        project = _project_or_422(view.workspace_id, data["project"])
        _require_edit(request, view.workspace_id, project)
    try:
        view = services.update(view, data=data, user=request.user, project=project, project_given=project_given)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return _out(_view_or_404(request, view.pk))


@router.delete("/{view_id}", response={204: None}, summary="Delete a dynamic view and its history")
@gate("agent.work", kind="content")
def delete_dynamic_view(request: HttpRequest, view_id: int):
    view = _view_or_404(request, view_id)
    _require_edit(request, view.workspace_id, view.project)
    view.delete()
    return Status(204, None)


@router.get("/{view_id}/revisions/", response=list[DynamicViewRevisionOut],
            summary="Every saved version of a dynamic view, newest first")
@gate("member")
def list_dynamic_view_revisions(request: HttpRequest, view_id: int) -> list[DynamicViewRevisionOut]:
    view = _view_or_404(request, view_id)
    return [DynamicViewRevisionOut(version=r.version, title=r.title, note=r.note,
                                   changed_by_email=r.changed_by.email if r.changed_by_id else None,
                                   changed_at=r.changed_at, code=r.code, sources=r.sources or [])
            for r in view.revisions.select_related("changed_by")]


@router.post("/{view_id}/rollback", response=DynamicViewOut,
             summary="Restore an earlier version of a dynamic view (as a new version)")
@gate("agent.work", kind="content")
def rollback_dynamic_view(request: HttpRequest, view_id: int, payload: DynamicViewRollbackIn) -> DynamicViewOut:
    view = _view_or_404(request, view_id)
    _require_edit(request, view.workspace_id, view.project)
    try:
        view = services.rollback(view, version=payload.version, user=request.user)
    except LookupError as exc:
        raise HttpError(404, str(exc)) from exc
    return _out(_view_or_404(request, view.pk))
