"""Django Ninja v2 router for the projects + insights surface."""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q
from django.http import HttpRequest
from ninja import Body, Router, Status

from apps.api.auth import session_auth
from apps.projects import services
from apps.workspaces import services as wsvc
from apps.api.errors import (
    TYPE_CONFLICT,
    TYPE_NOT_FOUND,
    ProblemError,
)
from apps.api.pagination import Page, clamp_limit, clamp_offset, paginate

from .models import Project, ProjectAction, ProjectContext
from .schemas import (
    BatchActionsIn,
    BatchContextIn,
    InsightDismissOut,
    InsightOut,
    InsightsClearIn,
    InsightsClearOut,
    ProjectActionCreateIn,
    ProjectActionOut,
    ProjectActionSummaryOut,
    ProjectContextCreateIn,
    ProjectContextEntryOut,
    ProjectContextLatestOut,
    ProjectContextOut,
    ProjectCreateIn,
    ProjectDetailOut,
    ProjectListOut,
    ProjectPatchIn,
    ProjectSlugOut,
)


def _build_project_list_data(qs):
    """Return [ProjectListOut-shaped dicts] for the given queryset.

    Pure Python replacement for the old DRF ProjectListSerializer — no
    rest_framework dependency. Produces the same dict shape so that
    ProjectListOut.model_validate() works unchanged.
    """
    qs = qs.select_related("created_by").prefetch_related(
        Prefetch(
            "contexts",
            queryset=ProjectContext.objects.order_by("-created_at"),
            to_attr="_prefetched_contexts",
        ),
        Prefetch(
            "actions",
            queryset=ProjectAction.objects.order_by("-started_at"),
            to_attr="_prefetched_actions",
        ),
    )

    result = []
    for p in qs:
        contexts = getattr(p, "_prefetched_contexts", None) or list(p.contexts.all())
        actions = getattr(p, "_prefetched_actions", None) or list(p.actions.all())

        # latest_context: first occurrence per context_type (prefetch is newest-first)
        latest_context: dict[str, dict] = {}
        insight_count = 0
        for ctx in contexts:
            if ctx.context_type == "insight":
                insight_count += 1
            if ctx.context_type not in latest_context:
                latest_context[ctx.context_type] = {
                    "content": ctx.content,
                    "source": ctx.source,
                    "created_at": ctx.created_at.isoformat(),
                }

        # latest_actions: most recent action per skill_name
        latest_actions: dict[str, dict] = {}
        for action in actions:
            if action.skill_name not in latest_actions:
                latest_actions[action.skill_name] = {
                    "status": action.status,
                    "started_at": action.started_at.isoformat(),
                    "completed_at": action.completed_at.isoformat() if action.completed_at else None,
                }

        from apps.walkthroughs.models import Walkthrough  # noqa: PLC0415
        walkthrough_count = Walkthrough.objects.filter(project_slug=p.slug).count()

        result.append({
            "id": p.pk,
            "name": p.name,
            "slug": p.slug,
            "repo_url": p.repo_url or "",
            "deploy_url": p.deploy_url or "",
            "visibility": p.visibility,
            "status": p.status,
            "skills": p.skills or [],
            "latest_context": latest_context,
            "latest_actions": latest_actions,
            "insight_count": insight_count,
            "walkthrough_count": walkthrough_count,
            "created_by_email": p.created_by.email if p.created_by_id else None,
            "created_at": p.created_at.isoformat(),
            "updated_at": p.updated_at.isoformat(),
        })
    return result

router = Router(auth=session_auth, tags=["projects"])
insights_router = Router(auth=session_auth, tags=["insights"])


def _scoped_project_queryset(request: HttpRequest):
    """Return a Project queryset limited to the caller's workspace scope.

    Mirrors the agents surface: when a workspace is pinned (the `/api/w/{ws}`
    prefix) filter to exactly that tenant; on the flat mount filter to every
    workspace the caller is a member of, plus any still-unscoped (null) rows.
    """
    ws = getattr(request, "workspace_slug", None)
    qs = Project.objects.all()
    if ws:
        return qs.filter(workspace_id=ws)
    slugs = wsvc.user_workspace_slugs(request.user)
    return qs.filter(Q(workspace_id__in=slugs) | Q(workspace__isnull=True))


def _member_project(request: HttpRequest, slug: str) -> Project | None:
    """Return the project if it exists and the caller may access it in the
    current workspace scope, else None (a non-member is indistinguishable from
    a missing project — no existence leak)."""
    project = Project.objects.filter(slug=slug).first()
    if project is None:
        return None
    ws = getattr(request, "workspace_slug", None)
    if ws and project.workspace_id != ws:
        return None  # wrong tenant
    if project.workspace_id and not wsvc.is_member(request.user, project.workspace_id):
        return None
    return project


def _get_project_or_404_ninja(request: HttpRequest, slug: str) -> Project:
    project = _member_project(request, slug)
    if project is None:
        raise ProblemError(
            404,
            "Project not found",
            type_=TYPE_NOT_FOUND,
            detail=f"No project with slug '{slug}'.",
        )
    return project


def _resolve_create_workspace(request: HttpRequest):
    """Resolve the workspace a newly created project belongs to and ensure the
    caller is a member. Uses the pinned `/api/w/{ws}` workspace when present,
    else the org default (so an unchanged flat client keeps working)."""
    pinned = getattr(request, "workspace_slug", None)
    ws = (
        wsvc.Workspace.objects.filter(slug=pinned).first() if pinned else None
    ) or wsvc.ensure_default_workspace()
    if ws is not None:
        wsvc.ensure_member(ws, request.user)
    return ws


def _project_to_detail_out(project: Project) -> ProjectDetailOut:
    """Build a ProjectDetailOut from a Project instance.

    Mirrors the DRF ProjectListSerializer shape: latest_context,
    latest_actions, insight_count, walkthrough_count are all computed
    from the related rows — no prefetch assumed here (safe for single-object
    calls; list paths use _build_project_list_data for efficiency).
    """
    from apps.walkthroughs.models import Walkthrough  # noqa: PLC0415

    # latest_context: first entry per context_type (newest-first ordering)
    latest_context: dict[str, ProjectContextOut] = {}
    for ctx in project.contexts.order_by("-created_at"):
        if ctx.context_type not in latest_context:
            latest_context[ctx.context_type] = ProjectContextOut(
                content=ctx.content,
                source=ctx.source,
                created_at=ctx.created_at,
            )

    # latest_actions: most recent action per skill_name
    from .schemas import ProjectActionLatestOut  # noqa: PLC0415

    latest_actions: dict[str, ProjectActionLatestOut] = {}
    for action in project.actions.order_by("-started_at"):
        if action.skill_name not in latest_actions:
            latest_actions[action.skill_name] = ProjectActionLatestOut(
                status=action.status,
                started_at=action.started_at,
                completed_at=action.completed_at,
            )

    insight_count = project.contexts.filter(context_type="insight").count()
    walkthrough_count = Walkthrough.objects.filter(project_slug=project.slug).count()

    skills_raw = project.skills or []
    from .schemas import ProjectSkillOut  # noqa: PLC0415

    skills = [
        ProjectSkillOut(
            name=s.get("name", ""),
            path=s.get("path", ""),
            description=s.get("description", ""),
        )
        for s in skills_raw
        if isinstance(s, dict)
    ]

    return ProjectDetailOut(
        id=project.pk,
        name=project.name,
        slug=project.slug,
        repo_url=project.repo_url or "",
        deploy_url=project.deploy_url or "",
        visibility=project.visibility,
        status=project.status,
        skills=skills,
        latest_context=latest_context,
        latest_actions=latest_actions,
        insight_count=insight_count,
        walkthrough_count=walkthrough_count,
        created_by_email=project.created_by.email if project.created_by_id else None,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


# ---------------------------------------------------------------------------
# Projects router
# ---------------------------------------------------------------------------


@router.get("/", response=Page[ProjectListOut], summary="List projects")
def list_projects(
    request: HttpRequest,
    offset: int = 0,
    limit: int = 100,
) -> Page[ProjectListOut]:
    offset, limit = clamp_offset(offset), clamp_limit(limit, cap=500)
    qs = _scoped_project_queryset(request).order_by("-updated_at")
    serialized = _build_project_list_data(qs)
    items = [ProjectListOut.model_validate(item) for item in serialized]
    return paginate(items, offset=offset, limit=limit)


@router.post("/", response={201: ProjectDetailOut}, summary="Create project")
def create_project(
    request: HttpRequest,
    payload: ProjectCreateIn,
) -> Status:
    ws = _resolve_create_workspace(request)
    try:
        # Own atomic block (savepoint): an IntegrityError from the duplicate-slug
        # constraint must not poison an outer transaction — e.g. the session write
        # SessionMiddleware makes on every response (SESSION_SAVE_EVERY_REQUEST)
        # would otherwise hit a broken connection and 500/400 instead of 409.
        with transaction.atomic():
            project = Project.objects.create(
                name=payload.name,
                slug=payload.slug,
                repo_url=payload.repo_url or "",
                deploy_url=payload.deploy_url or "",
                visibility=payload.visibility,
                status=payload.status,
                skills=[s.model_dump() for s in payload.skills],
                workspace=ws,
                created_by=request.user if request.user.is_authenticated else None,
            )
    except IntegrityError:
        raise ProblemError(
            409,
            "Slug already exists",
            type_=TYPE_CONFLICT,
            detail=f"A project with slug '{payload.slug}' already exists.",
        )
    return Status(201, _project_to_detail_out(project))


@router.get(
    "/slugs/",
    response=list[ProjectSlugOut],
    summary="List project slugs",
    openapi_extra={"x-mcp-expose": True},
)
def get_project_slugs(request: HttpRequest) -> list[ProjectSlugOut]:
    """Slim machine-readable slug list (Bearer-readable), workspace-scoped."""
    projects = (
        _scoped_project_queryset(request)
        .filter(status="active")
        .order_by("slug")
        .values("slug", "name", "status", "visibility", "workspace")
    )
    return [ProjectSlugOut.model_validate(p) for p in projects]


@router.post("/seed/", response={201: list[ProjectDetailOut]}, summary="Seed projects")
def seed_projects(
    request: HttpRequest,
    payload: list[ProjectCreateIn] = Body(...),
) -> Status:
    ws = _resolve_create_workspace(request)
    results = []
    for item in payload:
        project, _ = Project.objects.get_or_create(
            slug=item.slug,
            defaults={
                "name": item.name,
                "repo_url": item.repo_url or "",
                "deploy_url": item.deploy_url or "",
                "visibility": item.visibility,
                "status": item.status,
                "skills": [s.model_dump() for s in item.skills],
                "workspace": ws,
            },
        )
        results.append(_project_to_detail_out(project))
    return Status(201, results)


@router.post(
    "/batch-context/",
    response={201: dict},
    summary="Batch create context entries",
)
def batch_context(
    request: HttpRequest,
    payload: BatchContextIn,
) -> Status:
    """Create context entries across multiple projects. Bearer-writable xfail (Phase 5.4)."""
    counts: dict[str, int] = {}
    for slug, entries in payload.updates.items():
        project = _member_project(request, slug)
        if project is None:
            counts[slug] = 0
            continue
        created = 0
        for entry in entries:
            ProjectContext.objects.create(
                project=project,
                context_type=entry.context_type,
                content=entry.content,
                source=entry.source,
            )
            created += 1
        counts[slug] = created
    return Status(201, counts)


@router.post(
    "/batch-actions/",
    response={201: dict},
    summary="Batch create action entries",
)
def batch_actions(
    request: HttpRequest,
    payload: BatchActionsIn,
) -> Status:
    """Create action entries across multiple projects. Bearer-writable xfail (Phase 5.4)."""
    counts: dict[str, int] = {}
    for slug, entries in payload.updates.items():
        project = _member_project(request, slug)
        if project is None:
            counts[slug] = 0
            continue
        created = 0
        for entry in entries:
            ProjectAction.objects.create(
                project=project,
                skill_name=entry.skill_name,
                session_id=entry.session_id or "",
                status=entry.status,
                started_at=entry.started_at,
                completed_at=entry.completed_at,
                duration_ms=entry.duration_ms,
                notes=entry.notes or "",
            )
            created += 1
        counts[slug] = created
    return Status(201, counts)


@router.get("/{slug}/", response=ProjectDetailOut, summary="Get project detail")
def get_project(request: HttpRequest, slug: str) -> ProjectDetailOut:
    project = _get_project_or_404_ninja(request, slug)
    return _project_to_detail_out(project)


@router.patch("/{slug}/", response=ProjectDetailOut, summary="Patch project")
def patch_project(
    request: HttpRequest,
    slug: str,
    payload: ProjectPatchIn,
) -> ProjectDetailOut:
    project = _get_project_or_404_ninja(request, slug)
    updates = payload.model_dump(exclude_unset=True)
    if "skills" in updates and updates["skills"] is not None:
        updates["skills"] = [
            s.model_dump() if hasattr(s, "model_dump") else s
            for s in updates["skills"]
        ]
    for field, value in updates.items():
        setattr(project, field, value)
    project.save()
    return _project_to_detail_out(project)


@router.delete("/{slug}/", response={204: None}, summary="Delete project")
def delete_project(
    request: HttpRequest,
    slug: str,
) -> Status:
    project = _get_project_or_404_ninja(request, slug)
    project.delete()
    return Status(204, None)


@router.get(
    "/{slug}/context/",
    response=list[ProjectContextEntryOut],
    summary="List context entries",
)
def list_context(request: HttpRequest, slug: str) -> list[ProjectContextEntryOut]:
    project = _get_project_or_404_ninja(request, slug)
    contexts = project.contexts.order_by("-created_at")
    return [
        ProjectContextEntryOut(
            id=ctx.pk,
            context_type=ctx.context_type,
            content=ctx.content,
            source=ctx.source,
            created_at=ctx.created_at,
        )
        for ctx in contexts
    ]


@router.post(
    "/{slug}/context/",
    response={201: ProjectContextEntryOut},
    summary="Create context entry",
)
def create_context(
    request: HttpRequest,
    slug: str,
    payload: ProjectContextCreateIn,
) -> Status:
    """Bearer-writable xfail (Phase 5.4)."""
    project = _get_project_or_404_ninja(request, slug)
    ctx = ProjectContext.objects.create(
        project=project,
        context_type=payload.context_type,
        content=payload.content,
        source=payload.source,
    )
    return Status(201, ProjectContextEntryOut(
        id=ctx.pk,
        context_type=ctx.context_type,
        content=ctx.content,
        source=ctx.source,
        created_at=ctx.created_at,
    ))


@router.get(
    "/{slug}/context/latest/",
    response=ProjectContextLatestOut,
    summary="Latest context per type",
)
def get_context_latest(request: HttpRequest, slug: str) -> ProjectContextLatestOut:
    project = _get_project_or_404_ninja(request, slug)
    result: dict[str, ProjectContextOut] = {}
    for ctx in project.contexts.order_by("-created_at"):
        if ctx.context_type not in result:
            result[ctx.context_type] = ProjectContextOut(
                content=ctx.content,
                source=ctx.source,
                created_at=ctx.created_at,
            )
    return ProjectContextLatestOut(contexts=result)


@router.get(
    "/{slug}/actions/",
    response=list[ProjectActionOut],
    summary="List actions",
)
def list_actions(
    request: HttpRequest,
    slug: str,
    skill: str | None = None,
) -> list[ProjectActionOut]:
    project = _get_project_or_404_ninja(request, slug)
    actions = project.actions.all()
    if skill:
        actions = actions.filter(skill_name=skill)
    return [
        ProjectActionOut(
            id=action.pk,
            skill_name=action.skill_name,
            session_id=action.session_id or "",
            status=action.status,
            started_at=action.started_at,
            completed_at=action.completed_at,
            duration_ms=action.duration_ms,
            notes=action.notes or "",
            created_at=action.created_at,
        )
        for action in actions[:50]
    ]


@router.post(
    "/{slug}/actions/",
    response={201: ProjectActionOut},
    summary="Create action",
)
def create_action(
    request: HttpRequest,
    slug: str,
    payload: ProjectActionCreateIn,
) -> Status:
    """Bearer-writable xfail (Phase 5.4)."""
    project = _get_project_or_404_ninja(request, slug)
    action = ProjectAction.objects.create(
        project=project,
        skill_name=payload.skill_name,
        session_id=payload.session_id or "",
        status=payload.status,
        started_at=payload.started_at,
        completed_at=payload.completed_at,
        duration_ms=payload.duration_ms,
        notes=payload.notes or "",
    )
    return Status(201, ProjectActionOut(
        id=action.pk,
        skill_name=action.skill_name,
        session_id=action.session_id or "",
        status=action.status,
        started_at=action.started_at,
        completed_at=action.completed_at,
        duration_ms=action.duration_ms,
        notes=action.notes or "",
        created_at=action.created_at,
    ))


@router.get(
    "/{slug}/actions/summary/",
    response=list[ProjectActionSummaryOut],
    summary="Latest action per skill",
)
def get_actions_summary(
    request: HttpRequest,
    slug: str,
) -> list[ProjectActionSummaryOut]:
    project = _get_project_or_404_ninja(request, slug)
    seen: set[str] = set()
    result = []
    for action in project.actions.order_by("-started_at"):
        if action.skill_name not in seen:
            seen.add(action.skill_name)
            result.append(
                ProjectActionSummaryOut(
                    skill_name=action.skill_name,
                    status=action.status,
                    started_at=action.started_at,
                    completed_at=action.completed_at,
                )
            )
    return result


# ---------------------------------------------------------------------------
# Insights router
# ---------------------------------------------------------------------------


@insights_router.get(
    "/",
    response=Page[InsightOut],
    summary="List insights",
    openapi_extra={"x-mcp-expose": True},
)
def list_insights(
    request: HttpRequest,
    category: str | None = None,
    source: str | None = None,
    project: str | None = None,
    limit: int = 20,
) -> Page[InsightOut]:
    """Bearer-readable xfail (Phase 5.4)."""
    limit = clamp_limit(limit, cap=100)
    rows = services.list_insights(
        workspace_slugs=wsvc.request_workspace_slugs(request),
        category=category, source=source, project=project, limit=limit,
    )
    items = [InsightOut.model_validate(row) for row in rows]
    return paginate(items, offset=0, limit=limit)


@insights_router.post(
    "/clear/",
    response=InsightsClearOut,
    summary="Clear insights",
    openapi_extra={"x-mcp-expose": True},
)
def clear_insights(
    request: HttpRequest,
    payload: InsightsClearIn,
) -> InsightsClearOut:
    """Delete insights matching the provided filters.

    All filters in the request body are optional and AND-combined:
      - source: ProjectContext.source exact match
      - category: content starts with "[<category>]"
      - project: project slug exact match
      - older_than_days: created_at older than N days ago

    A body with no filters ({}) clears ALL insights — this is intended.
    """
    count = services.clear_insights(
        workspace_slugs=wsvc.request_workspace_slugs(request),
        source=payload.source,
        category=payload.category,
        project=payload.project,
        older_than_days=payload.older_than_days,
    )
    return InsightsClearOut(cleared=count)


@insights_router.delete("/{pk}/", response=InsightDismissOut, summary="Dismiss insight")
def dismiss_insight(request: HttpRequest, pk: int) -> InsightDismissOut:
    # Scope by the caller's workspaces (via the insight's project) so a member of
    # one workspace can't delete another's insight by enumerating pks. A row outside
    # scope is indistinguishable from a missing one → 404, no existence leak.
    insight = (
        services.insights_queryset(workspace_slugs=wsvc.request_workspace_slugs(request))
        .filter(pk=pk)
        .first()
    )
    if insight is None:
        raise ProblemError(
            404,
            "Insight not found",
            type_=TYPE_NOT_FOUND,
            detail=f"No insight with pk={pk}.",
        )
    insight.delete()
    return InsightDismissOut(dismissed=pk)
