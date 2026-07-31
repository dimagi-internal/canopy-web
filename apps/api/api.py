"""Single NinjaAPI instance for the /api/ namespace.

All v2 routers register against this. Routers live in
`apps/<app>/api.py` and are imported below.
"""
from __future__ import annotations

import logging

from django.http import Http404, HttpRequest, HttpResponse
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, HttpError, ValidationError

from .auth import session_auth
from .errors import (
    TYPE_AUTH,
    TYPE_INTERNAL,
    TYPE_NOT_FOUND,
    TYPE_VALIDATION,
    Problem,
    ProblemError,
)
from .renderers import OrjsonRenderer

logger = logging.getLogger(__name__)


api = NinjaAPI(
    title="canopy-web API",
    version="2.0.0",
    description=(
        "Pydantic-typed API surface for canopy-web. "
        "Replaces the legacy /api/ DRF endpoints. "
        "Errors are RFC 7807 application/problem+json."
    ),
    urls_namespace="api_v2",
    renderer=OrjsonRenderer(),
    docs_url=None,  # Scalar is mounted separately in config/urls.py
    openapi_url="/openapi.json",
)


def _problem_response(request: HttpRequest, problem: Problem) -> HttpResponse:
    body = problem.model_dump(exclude_none=True)
    response = HttpResponse(
        content=OrjsonRenderer().render(request, body, response_status=problem.status),
        status=problem.status,
        content_type="application/problem+json",
    )
    return response


@api.exception_handler(ProblemError)
def _on_problem_error(request: HttpRequest, exc: ProblemError) -> HttpResponse:
    problem = Problem(
        type=exc.problem_type,
        title=exc.problem_title,
        status=exc.status_code,
        detail=exc.problem_detail,
        instance=request.path,
        extras=exc.problem_extras,
    )
    return _problem_response(request, problem)


@api.exception_handler(ValidationError)
def _on_validation_error(request: HttpRequest, exc: ValidationError) -> HttpResponse:
    problem = Problem(
        type=TYPE_VALIDATION,
        title="Request validation failed",
        status=422,
        detail="One or more fields failed validation.",
        instance=request.path,
        extras={"errors": exc.errors},
    )
    return _problem_response(request, problem)


@api.exception_handler(AuthenticationError)
def _on_auth_error(request: HttpRequest, exc: AuthenticationError) -> HttpResponse:
    problem = Problem(
        type=TYPE_AUTH,
        title="Authentication required",
        status=401,
        detail="This endpoint requires an authenticated session.",
        instance=request.path,
    )
    return _problem_response(request, problem)


@api.exception_handler(HttpError)
def _on_http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
    """Bare HttpError (raised from handlers using ninja's shortcut) → problem+json."""
    problem = Problem(
        type="about:blank",
        title=exc.message if hasattr(exc, "message") else "HTTP error",
        status=exc.status_code,
        detail=str(exc) if str(exc) else None,
        instance=request.path,
    )
    return _problem_response(request, problem)


@api.exception_handler(Http404)
def _on_http404(request: HttpRequest, exc: Http404) -> HttpResponse:
    """Django Http404 (from get_object_or_404) → problem+json."""
    problem = Problem(
        type=TYPE_NOT_FOUND,
        title="Not found",
        status=404,
        detail=str(exc) if str(exc) else None,
        instance=request.path,
    )
    return _problem_response(request, problem)


@api.exception_handler(Exception)
def _on_unhandled(request: HttpRequest, exc: Exception) -> HttpResponse:
    logger.exception("Unhandled exception in v2 handler")
    problem = Problem(
        type=TYPE_INTERNAL,
        title="Internal server error",
        status=500,
        detail="An unexpected error occurred.",
        instance=request.path,
    )
    return _problem_response(request, problem)


@api.get("/_auth_smoke/", auth=session_auth, response={200: dict})
def _auth_smoke(request: HttpRequest) -> dict:
    """Internal smoke route — verifies session auth works."""
    return {"email": getattr(request.user, "email", "")}


from apps.common.api import ai_router, common_router, public_router  # noqa: E402
from apps.projects.api import insights_router  # noqa: E402
from apps.projects.api import router as projects_router  # noqa: E402
from apps.issues.api import router as issues_router  # noqa: E402
from apps.tokens.api import router as tokens_router  # noqa: E402
from apps.reviews.api import router as reviews_router  # noqa: E402
from apps.runs.api import router as runs_router  # noqa: E402
from apps.walkthroughs.api import router as walkthroughs_router  # noqa: E402
from apps.shareouts.api import router as shareouts_router  # noqa: E402
from apps.session_sharing.api import router as sessions_router  # noqa: E402
from apps.session_sharing.api import share_router as session_share_router  # noqa: E402
from apps.agents.api import router as agents_router  # noqa: E402
from apps.agent_runs.api import router as agent_runs_router  # noqa: E402
from apps.workspaces.api import router as workspaces_router  # noqa: E402
from apps.timeline.api import router as timeline_router  # noqa: E402
from apps.system.api import router as system_router  # noqa: E402
from apps.harness.api import router as harness_router  # noqa: E402
from apps.events.api import router as events_router  # noqa: E402
from apps.inbound.api import router as inbound_router  # noqa: E402
from apps.feedback.api import router as feedback_router  # noqa: E402
from apps.storyboards.api import router as storyboards_router  # noqa: E402
from apps.harness.api_schedules import router as schedules_router  # noqa: E402
from apps.harness.items_api import (  # noqa: E402
    agent_items_router,
    items_router,
)
from apps.push.api import router as push_router  # noqa: E402
from apps.canopy_sessions.api import router as canopy_sessions_router  # noqa: E402
from apps.tokens.exchange_api import exchange_router  # noqa: E402

api.add_router("/projects", projects_router)
api.add_router("/issues", issues_router)
api.add_router("/insights", insights_router)
api.add_router("/ai", ai_router)
api.add_router("", common_router)
api.add_router("", public_router)
api.add_router("/walkthroughs", walkthroughs_router)
api.add_router("/tokens", tokens_router)
api.add_router("/feedback", feedback_router)
api.add_router("/events", events_router)
api.add_router("/inbound", inbound_router)
api.add_router("/storyboards", storyboards_router)
api.add_router("/auth", exchange_router)
api.add_router("/reviews", reviews_router)
api.add_router("/ddd", runs_router)
api.add_router("/shareouts", shareouts_router)
api.add_router("/sessions", sessions_router)
# Agents are workspace-scoped. Mounted flat (single, clean schema); the canonical
# tenant URL /api/w/{ws}/agents/... is gated + stripped to this flat path by
# apps.api.tenancy.WorkspaceResolveMiddleware, which also sets request.workspace_slug.
api.add_router("/agents", agents_router)
api.add_router("/agents", agent_runs_router)  # unified run lifecycle under the agents namespace
api.add_router("/agents", schedules_router)  # recurring turns, under the agents namespace
# Items — the supervisor's queue (the dual of Turn). The collection is
# agent-scoped ("whose queue?"), the resource is not (an item id is global).
api.add_router("/agents", agent_items_router)
api.add_router("/items", items_router)
api.add_router("/workspaces", workspaces_router)
api.add_router("/timeline", timeline_router)
api.add_router("/system", system_router)
api.add_router("/share", session_share_router)
api.add_router("/harness", harness_router)
api.add_router("/push", push_router)
api.add_router("/canopy-sessions", canopy_sessions_router)
