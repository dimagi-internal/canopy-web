"""Single NinjaAPI instance for the /api/ namespace.

All v2 routers register against this. Routers live in
`apps/<app>/api.py` and are imported below.
"""
from __future__ import annotations

import inspect
import logging

from django.http import Http404, HttpRequest, HttpResponse
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, HttpError, ValidationError

from apps.api.route_gates import gate

from .auth import session_auth
from .errors import (
    TYPE_AUTH,
    TYPE_INTERNAL,
    TYPE_NOT_FOUND,
    TYPE_VALIDATION,
    Problem,
    ProblemError,
    status_title,
)
from .renderers import OrjsonRenderer

logger = logging.getLogger(__name__)


class CanopyNinjaAPI(NinjaAPI):
    """NinjaAPI whose operationIds are the view's own name.

    Ninja's default is ``<module>_<function>`` (``apps_agents_api_list_agents``).
    The operationId is also the MCP tool's name (``apps/mcp/api_tools.py``), so
    one name now serves the schema, ``generated.ts`` and the MCP surface. A name
    two modules share is qualified by its app (``chat_sessions_send``) so it
    stays unique — and it is the SAME rule for every name, not a hand-kept map.
    """

    def get_openapi_operation_id(self, operation) -> str:
        name = operation.view_func.__name__
        if name in self._shared_view_names():
            return f"{_app_label(operation.view_func.__module__)}_{name}"
        return name

    def _shared_view_names(self) -> frozenset[str]:
        cached = getattr(self, "_shared_names_cache", None)
        if cached is not None:
            return cached
        modules: dict[str, set[str]] = {}
        for bound in self._get_bound_routers():
            for path_view in bound.path_operations.values():
                for op in path_view.operations:
                    fn = op.view_func
                    modules.setdefault(fn.__name__, set()).add(fn.__module__)
        shared = frozenset(n for n, mods in modules.items() if len(mods) > 1)
        self._shared_names_cache = shared
        return shared

    def explicit_operation_ids(self) -> dict:
        """Unwrapped view function -> the ``operation_id=`` its route spells out."""
        cached = getattr(self, "_explicit_ids_cache", None)
        if cached is not None:
            return cached
        explicit = {
            inspect.unwrap(op.view_func): op.operation_id
            for bound in self._get_bound_routers()
            for path_view in bound.path_operations.values()
            for op in path_view.operations
            if op.operation_id
        }
        self._explicit_ids_cache = explicit
        return explicit


def _app_label(module: str) -> str:
    """``apps.canopy_sessions.contact_api`` → ``canopy_sessions_contact``.

    A module that moved apps keeps the label it was published under by naming it
    in ``OPERATION_LABEL`` — an operationId is an MCP tool name and a
    ``generated.ts`` key, so moving code must not rename it."""
    import sys

    pinned = getattr(sys.modules.get(module), "OPERATION_LABEL", None)
    if pinned:
        return pinned
    parts = module.split(".")
    if parts[0] == "apps":
        parts = parts[1:]
    if parts and parts[-1] == "api":
        parts = parts[:-1]
    return "_".join(p.removesuffix("_api") for p in parts)


api = CanopyNinjaAPI(
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

# Every route is wrapped by the settings audit (`apps/events/audit.py`): a no-op
# except on the routes declared `@gate(..., setting=True)`, where the change and its
# `SettingChange` row commit together. Registered here, before any router is
# added, so it reaches every route — and so every entry point (web UI, CLI,
# script, MCP tool) through the one place they all pass.
from apps.events.audit import audited as _audited  # noqa: E402

api.add_decorator(_audited)


def _problem_response(request: HttpRequest, problem: Problem, members: dict | None = None) -> HttpResponse:
    # Extension members sit beside the standard ones and never replace them.
    body = {**(members or {}), **problem.model_dump(exclude_none=True)}
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
    response = _problem_response(request, problem, getattr(exc, "problem_members", None))
    for name, value in (getattr(exc, "problem_headers", None) or {}).items():
        response[name] = value
    return response


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
    """Bare HttpError (raised from handlers using ninja's shortcut) → problem+json.

    `type: about:blank`, so `title` is the status's reason phrase (stable per
    type, RFC 7807 §4.2) and the message raised is the `detail`."""
    problem = Problem(
        type="about:blank",
        title=status_title(exc.status_code),
        status=exc.status_code,
        detail=str(exc) or None,
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
@gate("authenticated")  # internal smoke route
def _auth_smoke(request: HttpRequest) -> dict:
    """Internal smoke route — verifies session auth works."""
    return {"email": getattr(request.user, "email", "")}


from apps.agent_runs.api import router as agent_runs_router  # noqa: E402
from apps.agent_runs.documents import router as agent_run_docs_router  # noqa: E402
from apps.agents.a2a_api import router as a2a_router  # noqa: E402
from apps.agents.api import router as agents_router  # noqa: E402
from apps.agents.api import turns_router as agent_turns_router  # noqa: E402
from apps.agents.api_board import router as agent_board_router  # noqa: E402
from apps.agents.api_credentials import router as agent_credentials_router  # noqa: E402
from apps.agents.api_github import router as agent_github_router  # noqa: E402
from apps.agents.api_pat_rotation import router as agent_pat_rotation_router  # noqa: E402
from apps.agents.api_secret_requests import router as agent_secret_requests_router  # noqa: E402
from apps.agents.api_sender_trust import router as agent_sender_trust_router  # noqa: E402
from apps.agents.api_vault_members import router as agent_vault_members_router  # noqa: E402
from apps.agents.api_vault_zk import router as agent_vault_zk_router  # noqa: E402
from apps.agents.api_readiness import router as agent_readiness_router  # noqa: E402
from apps.agents.api_access_list import defaults_router as agent_defaults_router  # noqa: E402
from apps.agents.api_access_list import router as agent_access_list_router  # noqa: E402
from apps.agents.api_routing import router as agent_routing_router  # noqa: E402
from apps.agents.api_skills import router as agent_skills_router  # noqa: E402
from apps.agents.api_vault_keeper import router as vault_keeper_router  # noqa: E402
from apps.agents.api_vaults import router as workspace_vaults_router  # noqa: E402
from apps.agents.contributors_api import router as contributors_router  # noqa: E402
from apps.agents.credentials.oauth_api import oauth_router as google_oauth_router  # noqa: E402
from apps.agents.credentials.oauth_api import router as agent_google_router  # noqa: E402
from apps.agents.api_aws import router as agent_aws_router  # noqa: E402
from apps.agents.engagement_api import router as engagement_router  # noqa: E402
from apps.agents.fleet_api import router as fleet_tasks_router  # noqa: E402
from apps.agents.tasks_api import router as workspace_tasks_router  # noqa: E402
from apps.agents.kb_api import router as kb_router  # noqa: E402
from apps.agents.proposals_api import router as proposals_router  # noqa: E402
from apps.attention.alerts_api import router as alerts_router  # noqa: E402
from apps.attention.api import router as attention_router  # noqa: E402
from apps.agents.api_login_requests import router as agent_login_requests_router  # noqa: E402
from apps.harness.api_act_as import router as act_as_router  # noqa: E402
from apps.beta_requests.api import router as beta_requests_router  # noqa: E402
from apps.canopy_sessions.api import me_router as my_sessions_router  # noqa: E402
from apps.canopy_sessions.api import router as canopy_sessions_router  # noqa: E402
from apps.canopy_sessions.app_views_api import router as app_views_router  # noqa: E402
from apps.canopy_sessions.secrets_api import router as session_secrets_router  # noqa: E402
from apps.canopy_sessions.sharing_api import router as session_sharing_router  # noqa: E402
from apps.agent_tasks.api import router as agent_tasks_router  # noqa: E402
from apps.capabilities.api import agents_router as agent_capabilities_router  # noqa: E402
from apps.capabilities.api import router as capabilities_router  # noqa: E402
from apps.capabilities.api import runner_router as capability_keeper_router  # noqa: E402
from apps.common.api import common_router, public_router  # noqa: E402
from apps.connections.api import agents_router as connections_agents_router  # noqa: E402
from apps.connections.api import me_router as connections_me_router  # noqa: E402
from apps.connections.api import router as connections_router  # noqa: E402
from apps.connections.api import workspaces_router as connection_apps_router  # noqa: E402
from apps.connections.api_turns import router as connections_turns_router  # noqa: E402
from apps.contacts.api import router as contacts_router
from apps.contacts.contact_api import contact_router, contact_token_router  # noqa: E402
from apps.contacts.hcp_api import router as hcp_router  # noqa: E402
from apps.contacts.hcp_clients_api import router as hcp_clients_router  # noqa: E402
from apps.contacts.people_api import router as people_router  # noqa: E402
from apps.embedding.connected_apps_api import connected_apps_router  # noqa: E402
from apps.host_grants.api import needs_router as site_needs_router  # noqa: E402
from apps.contacts.member_profile_api import router as member_profile_router  # noqa: E402
from apps.host_grants.api import sites_router  # noqa: E402
from apps.host_grants.sign_offs_api import router as sign_offs_router  # noqa: E402
from apps.host_grants.sign_offs_api import sites_router as site_sign_offs_router  # noqa: E402
from apps.host_grants.sign_offs_api import embed_router as embed_sign_offs_router  # noqa: E402
from apps.host_grants.grants_api import embed_grant_router  # noqa: E402
from apps.host_grants.grants_api import me_router as host_grants_me_router  # noqa: E402
from apps.agents.api_delegated import me_router as delegated_me_router  # noqa: E402
from apps.embedding.embed_api import embed_router
from apps.events.api import router as events_router  # noqa: E402
from apps.feedback.api import router as feedback_router  # noqa: E402
from apps.github_connect.api import router as github_router  # noqa: E402
from apps.harness.api import router as harness_router  # noqa: E402
from apps.harness.api_device_key_requests import router as device_key_requests_router  # noqa: E402
from apps.harness.api_device_keys import router as device_keys_router  # noqa: E402
from apps.harness.api_investigations import router as investigations_router  # noqa: E402
from apps.harness.api_local import local_turns_router, private_runners_router  # noqa: E402
from apps.harness.api_project_opt_in import router as project_opt_in_router  # noqa: E402
from apps.harness.api_runner_invitations import pair_router as runner_pair_by_role_router  # noqa: E402
from apps.harness.api_box_egress import box_egress_router  # noqa: E402
from apps.harness.api_runner_invitations import router as runner_invitations_router  # noqa: E402
from apps.harness.api_runner_credential_sets import router as runner_credential_sets_router  # noqa: E402
from apps.harness.api_runner_fleets import router as runner_fleets_router  # noqa: E402
from apps.harness.api_runner_pairings import router as runner_pairings_router  # noqa: E402
from apps.harness.api_runner_releases import router as runner_releases_router  # noqa: E402
from apps.harness.api_runner_sessions import router as runner_sessions_router  # noqa: E402
from apps.harness.api_schedules import router as schedules_router  # noqa: E402
from apps.harness.api_turn_agent_token import router as turn_agent_token_router  # noqa: E402
from apps.huddles.api import router as huddles_router  # noqa: E402
from apps.huddles.checkins_api import project_router as checkins_project_router  # noqa: E402
from apps.huddles.checkins_api import router as checkins_router  # noqa: E402
from apps.inbound.api import router as inbound_router  # noqa: E402
from apps.issues.api import router as issues_router  # noqa: E402
from apps.mail.api import router as mail_router  # noqa: E402
from apps.oauth_server.api import router as oauth_connected_router  # noqa: E402
from apps.passkeys.api import router as passkeys_router  # noqa: E402
from apps.passkeys.api_vault import router as vault_keyset_router  # noqa: E402
from apps.push.api import router as push_router  # noqa: E402
from apps.approvals.api import router as approvals_router  # noqa: E402
from apps.reviews.api import router as reviews_router  # noqa: E402
from apps.runs.api import router as runs_router  # noqa: E402
from apps.session_sharing.api import router as sessions_router  # noqa: E402
from apps.session_sharing.api import share_router as session_share_router  # noqa: E402
from apps.shareouts.api import router as shareouts_router  # noqa: E402
from apps.dynamic_views.api import router as dynamic_views_router  # noqa: E402
from apps.workspace_apps.api import public_router as public_apps_router  # noqa: E402
from apps.workspace_apps.api import router as workspace_apps_router  # noqa: E402
from apps.slack.api import router as slack_config_router  # noqa: E402
from apps.slack.ingest_api import router as slack_ingests_router  # noqa: E402
from apps.slack.session_api import router as slack_session_router  # noqa: E402
from apps.storyboards.api import router as storyboards_router  # noqa: E402
from apps.system.api import router as system_router  # noqa: E402
from apps.threads.api import router as threads_router  # noqa: E402
from apps.timeline.api import router as timeline_router  # noqa: E402
from apps.tokens.api import router as tokens_router  # noqa: E402
from apps.walkthroughs.api import router as walkthroughs_router  # noqa: E402
from apps.workspaces.api import router as workspaces_router  # noqa: E402

api.add_router("/issues", issues_router)
api.add_router("", common_router)
api.add_router("", public_router)
api.add_router("/walkthroughs", walkthroughs_router)
api.add_router("/tokens", tokens_router)
# The MCP clients a person signed in (apps/oauth_server), under /api/tokens/connected-apps.
api.add_router("/tokens", oauth_connected_router)
# A person's GitHub connection: its own app, still under /api/tokens/github (and
# the `tokens` tag) where its routes and MCP tool names were published.
api.add_router("/tokens", github_router)
api.add_router("/connections", connections_router)  # server-held OAuth connections (Salesforce first)
api.add_router("/me/connections", connections_me_router)  # /api/me/connections/{provider}/token — before "/me"
api.add_router("/feedback", feedback_router)
api.add_router("/events", events_router)
api.add_router("/inbound", inbound_router)
api.add_router("/mail", mail_router)
api.add_router("/slack-config", slack_config_router)
api.add_router("/slack-ingests", slack_ingests_router)
api.add_router("/storyboards", storyboards_router)
api.add_router("", beta_requests_router)
api.add_router("/embed", embed_router)
api.add_router("/embed", embed_grant_router)  # the panel's "can read as you · Stop"
api.add_router("", host_grants_me_router)  # /api/me/host-grants/
api.add_router("", delegated_me_router)  # /api/me/delegated-credentials/
# The contact surface. `/api/contact/` is the ONLY prefix a contact token can
# reach — see apps/contacts/contact_api.py and the allowlist in
# apps/common/middleware.py. `/api/auth/contact-token` mints one and is
# unauthenticated because the signed assertion is the credential.
api.add_router("/contact", contact_router)
api.add_router("/auth", contact_token_router)
api.add_router("/reviews", reviews_router)
api.add_router("/ddd", runs_router)
api.add_router("/shareouts", shareouts_router)
api.add_router("/dynamic-views", dynamic_views_router)
api.add_router("", workspace_apps_router)
api.add_router("", public_apps_router)
api.add_router("/sessions", sessions_router)
# Agents are workspace-scoped. Mounted flat (single, clean schema); the canonical
# tenant URL /api/w/{ws}/agents/... is gated + stripped to this flat path by
# apps.api.tenancy.WorkspaceResolveMiddleware, which also sets request.workspace_slug.
# Before the agents router: `…/projects/propose/` must not be read as a project ref.
api.add_router("/agents", proposals_router)  # proposed projects + backings (layer 1, 2026-10-16)
api.add_router("/agents", checkins_project_router)  # a project's check-ins + its timeline (2026-10-17)
api.add_router("/agents", agents_router)
# The rest of apps/agents' surface, split by subdomain. Mounted in the order the
# routes were written in, which is the order of /api/openapi.json's paths.
api.add_router("/agents", agent_routing_router)
api.add_router("/agents", agent_access_list_router)
api.add_router("/workspaces", agent_defaults_router)
api.add_router("/agents", agent_turns_router)
api.add_router("/agents", agent_skills_router)
api.add_router("/agents", agent_board_router)
api.add_router("/agents", agent_credentials_router)
# The rest of an agent's secrets, split out of `api_credentials`. None of these paths can be
# shadowed by `/{slug}/credentials/{name}` or shadow it, so their order among themselves is free.
api.add_router("/agents", agent_vault_zk_router)
api.add_router("/agents", agent_vault_members_router)
api.add_router("/agents", agent_secret_requests_router)
api.add_router("/agents", agent_sender_trust_router)
api.add_router("/agents", agent_github_router)
api.add_router("/agents", agent_pat_rotation_router)
api.add_router("/agents", connections_agents_router)  # an agent's connections + grants
api.add_router("/agents", agent_readiness_router)
# A2A Agent Card discovery — public in the login middleware; each route
# self-enforces (apps/agents/a2a_api.py).
api.add_router("/a2a", a2a_router)
api.add_router("/agents", agent_runs_router)  # unified run lifecycle under the agents namespace
api.add_router("/agent-runs", agent_run_docs_router)  # run documents (DDD runs), cross-agent
api.add_router("/agents", agent_google_router)  # start the Google mailbox mint
api.add_router("/agents", agent_aws_router)  # one hour of a named read-only AWS role (labs-observe)
# The mint's callback is NOT agent-scoped: Google requires every redirect URI
# to be registered exactly, so one fixed path serves the whole fleet and the
# agent rides in signed state. A per-agent URI would mean a console edit per
# new agent — the barrier this feature exists to remove.
api.add_router("/oauth", google_oauth_router)
api.add_router("/agents", schedules_router)  # recurring turns, under the agents namespace
api.add_router("/agents", contributors_router)  # ideas + contributor pages on a project (hal T69)
api.add_router("/agents", agent_capabilities_router)  # what an agent declares it can lease
api.add_router("/capabilities", capabilities_router)  # grants + leases (cloud capabilities)
api.add_router("/agent-tasks", agent_tasks_router)  # call an agent inline, get a structured result
api.add_router("/passkeys", passkeys_router)  # a person's passkeys + the step-up (canopy-web#1538)
api.add_router("/vault", vault_keyset_router)  # a person's zero-knowledge vault key (canopy-web#1550)
# Fleet-wide task and project lists (/api/tasks/, /api/projects/) — the
# per-agent filters across every agent the caller can see.
api.add_router("", fleet_tasks_router)
# One task by key, bulk edits, relations, Triage, labels, saved views, task
# settings, a project's milestones and updates. After the lists: `/tasks/count/`
# must not be read as a key.
api.add_router("", workspace_tasks_router)
api.add_router("", kb_router)
api.add_router("/workspaces", workspaces_router)
# Tenant-admin surface, same prefix and shape as members/invites (apps/tokens).
api.add_router("/workspaces", connected_apps_router)
api.add_router("/workspaces", connection_apps_router)  # a workspace's OAuth clients for connections
api.add_router("/workspaces", site_needs_router)
api.add_router("/workspaces", member_profile_router)  # one member's page, by the caller's role
api.add_router("/sites", sites_router)
api.add_router("/sites/sign-offs", site_sign_offs_router)
api.add_router("/sign-offs", sign_offs_router)
api.add_router("/embed/sign-offs", embed_sign_offs_router)  # the same, decided in the site's frame
api.add_router("/timeline", timeline_router)
api.add_router("/system", system_router)
api.add_router("/share", session_share_router)
# Pairing invitations (a new cloud box pairs by its instance role, no PAT).
# Before the harness router: its `/runners/{runner_id}` would shadow
# `/runners/pair-by-role`.
api.add_router("/harness/runner-invitations", runner_invitations_router)
api.add_router("/harness/runner-fleets", runner_fleets_router)
api.add_router("/harness/runner-credential-sets", runner_credential_sets_router)
api.add_router("/harness/runners/pair-by-role", runner_pair_by_role_router)
api.add_router("/harness/runners/box-egress", box_egress_router)
# A laptop box pairs by browser, and its installers and updates (spec
# 2026-10-19-runner-installers).
api.add_router("/harness/runner-pairings", runner_pairings_router)
api.add_router("/harness/runner-releases", runner_releases_router)
# A turn-scoped agent login (spec 2026-10-21-turn-scoped-agent-token).
api.add_router("/harness/runners", turn_agent_token_router)

# Direct use (canopy-web#1704): a person's private runner and the turns its sessions open.
api.add_router("/harness/local-turns", local_turns_router)
api.add_router("/private-runners", private_runners_router)
api.add_router("", workspace_vaults_router)  # workspace vaults: /workspaces/{slug}/vaults, /vaults/{id}
api.add_router("/harness", vault_keeper_router)  # the keeper's calls, under the runner's own path
api.add_router("/harness", capability_keeper_router)  # … and its slot-secret calls
api.add_router("/harness", connections_turns_router)  # a turn's agent connection token (apps/connections)
api.add_router("/harness", harness_router)
api.add_router("/me/canopy-projects", project_opt_in_router)
# What the fleet's debugger agent reads and closes (apps/harness/auto_debug.py).
api.add_router("/harness/failure-investigations", investigations_router)
# Canopy Vault: hardware-bound runner keys behind the DPoP device proof (#1538).
api.add_router("/runner-device-keys", device_keys_router)
api.add_router("/runner-sessions", runner_sessions_router)
api.add_router("/runner-device-key-requests", device_key_requests_router)
api.add_router("/huddles", huddles_router)
api.add_router("/me/attention", attention_router)
api.add_router("/me/agent-login-requests", agent_login_requests_router)
api.add_router("/me/act-as-requests", act_as_router)  # an agent acting as its turn's person
api.add_router("/me/alerts", alerts_router)
api.add_router("/engagement", engagement_router)  # what people do with projects + huddles
# Project check-ins across projects, and their runs (apps/huddles/checkins.py).
api.add_router("/checkins", checkins_router)
# The two work-group run routes an un-updated engine still calls — one release only.
api.add_router("/threads", threads_router)
api.add_router("/push", push_router)
api.add_router("/approvals", approvals_router)
# A session's Slack thread (share / detach / new-thread) — Slack's own routes, at
# the session paths they always had. Mounted first: fixed `/slack/...` paths.
api.add_router("/canopy-sessions", slack_session_router)
api.add_router("/canopy-sessions", canopy_sessions_router)
api.add_router("/me/session-sharing", session_sharing_router)
api.add_router("/me", my_sessions_router)
# MCP Apps: a rendered View's browser plumbing (apps/mcp_apps_host/mcp_apps_views.py).
api.add_router("/canopy-sessions", app_views_router)
api.add_router("/session-secrets", session_secrets_router)
api.add_router("/contacts", contacts_router)
api.add_router("/people", people_router)  # fleet brain v1 (canopy#804)
api.add_router("/hcp", hcp_router)  # Human Context Protocol v1 over the people brain
api.add_router("/hcp-admin", hcp_clients_router)  # the HCP service's client registry (superusers)
