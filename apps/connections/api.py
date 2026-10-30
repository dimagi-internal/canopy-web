"""The JSON half of connections: a workspace's apps, status, disconnect, sharing,
and my CLI token.

Errors from the service layer become responses in one place, `problem_for`.

Four routers, because the URLs live under four prefixes (see `apps/api/api.py`):
`router` at `/connections` (a person's own), `agents_router` at `/agents`
(an agent's, admin-only), `me_router` at `/me/connections` (the CLI token) and
`workspaces_router` at `/workspaces` (a workspace's apps, its admins').
The browser legs of connecting are bare views (`views.py`).
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router, Status
from ninja.errors import HttpError

from apps.agents.gates import agent_for_admin, agent_for_read
from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.errors import ProblemError
from apps.api.route_gates import gate
from apps.passkeys import stepup
from apps.tokens import kinds
from apps.workspaces import permissions as perms
from apps.workspaces.gates import workspace_for

from . import providers, services
from .models import Connection, ConnectionApp, ConnectionGrant
from .problems import problem_for
from .schemas import (
    AccessTokenOut,
    ConnectionAppIn,
    ConnectionAppOut,
    ConnectionAppsOut,
    ConnectionOut,
    ConnectionsOut,
    OptionFieldOut,
    ProviderOut,
    ScopeChoiceOut,
)

router = Router(auth=session_auth, tags=["connections"])
agents_router = Router(auth=session_auth, tags=["connections"])
me_router = Router(auth=session_auth, tags=["connections"])
workspaces_router = Router(auth=session_auth, tags=["connections"])


def _person_provider_rows(user) -> list[ProviderOut]:
    # One row per (workspace I can read with the provider set up, provider); a
    # provider set up nowhere of mine still lists once, unconfigured.
    rows = []
    for c in providers.CLASSES.values():
        found = services.apps_for_person(user, c.name)
        rows += [ProviderOut(name=c.name, title=c.title, workspace=a.workspace_id,
                             workspace_name=a.workspace.display_name, configured=True) for a in found]
        if not found:
            rows.append(ProviderOut(name=c.name, title=c.title, configured=False))
    return rows


def _agent_provider_rows(agent: Agent) -> list[ProviderOut]:
    # The agent's own workspace's apps, set up or not: the only ones it connects with.
    apps = {a.provider: a for a in ConnectionApp.objects.filter(workspace_id=agent.workspace_id)}
    name = agent.workspace.display_name
    return [ProviderOut(name=c.name, title=c.title, workspace=agent.workspace_id, workspace_name=name,
                        configured=c.name in apps and providers.for_app(apps[c.name]).is_configured())
            for c in providers.CLASSES.values()]


def _title(provider: str) -> str:
    # A row for a provider no longer registered still lists (and can be disconnected).
    return getattr(providers.CLASSES.get(provider), "title", provider)


def _out(conn: Connection, *, granted_from: str | None = None) -> ConnectionOut:
    return ConnectionOut(
        id=conn.pk, provider=conn.provider, title=_title(conn.provider),
        workspace=conn.app.workspace_id, workspace_name=conn.app.workspace.display_name,
        account_label=conn.account_label, instance_url=conn.instance_url, scopes=conn.scopes.split(),
        needs_reconnect=conn.needs_reconnect, connected_at=conn.created_at,
        last_used_at=conn.last_used_at,
        connected_by_email=((conn.connected_by.email or conn.connected_by.get_username())
                            if conn.connected_by is not None else ""),
        granted_from=granted_from,
        # Agent-owned, not itself a shared row. Callers prefetch `grants__agent`.
        shared_with=(sorted(g.agent.slug for g in conn.grants.all())
                     if conn.agent_id and granted_from is None else []),
    )


def _start_url(request: HttpRequest, provider: str, workspace: str) -> str:
    # Absolute: the CLI prints it for a person to open, far from any page.
    return request.build_absolute_uri(reverse("connections_start", args=[provider, workspace]))


def _callback_url(request: HttpRequest, provider: str, workspace: str) -> str:
    return request.build_absolute_uri(reverse("connections_callback", args=[provider, workspace]))


def _provider_or_404(provider: str) -> type[providers.Provider]:
    try:
        return providers.get_class(provider)
    except providers.UnknownProvider:
        raise HttpError(404, "unknown provider") from None


# ---- a person's own ------------------------------------------------------------

@router.get("/", response=ConnectionsOut, summary="My connections")
@gate("self")
def my_connections(request: HttpRequest) -> ConnectionsOut:
    """The services I can connect to, and the accounts I have connected.

    A provider lists once per workspace of mine that has set it up (connect with
    that workspace's app), or once as not set up. Never calls the provider, so
    it is cheap to read on every visit to Settings. Whether a grant still works
    shows as `needs_reconnect`, which the next real use of it keeps up to date."""
    rows = (services.connections_for(("user", request.user.pk))
            .select_related("connected_by", "app__workspace"))
    return ConnectionsOut(providers=_person_provider_rows(request.user), connections=[_out(c) for c in rows])


@router.delete("/{connection_id}", response={204: None}, summary="Disconnect one of my connections")
@gate("self", setting=True)
def disconnect_mine(request: HttpRequest, connection_id: int) -> Status:
    """Stop canopy holding this connection, and ask the provider to revoke it.

    Only my own: someone else's id is a 404, not a 403, so ids cannot be probed."""
    conn = get_object_or_404(Connection, pk=connection_id, user=request.user)
    services.disconnect(conn, by=request.user)
    return Status(204, None)


# ---- an agent's ------------------------------------------------------------------

@agents_router.get("/{slug}/connections", response=ConnectionsOut,
                   summary="An agent's connections, its own and shared with it")
@gate("agent-admin")
def agent_connections(request: HttpRequest, slug: str) -> ConnectionsOut:
    """What this agent can act as: connections it owns, and ones another agent
    shares with it (`granted_from` names the owner), and the providers its own
    workspace has set up, which are what it connects with. Only the agent's admins can see them."""
    agent = agent_for_admin(request, slug)
    own = (services.connections_for(("agent", agent.pk)).select_related("connected_by", "app__workspace")
           .prefetch_related("grants__agent"))
    granted = (Connection.objects.filter(grants__agent=agent)
               .select_related("connected_by", "agent", "app__workspace")
               .order_by("provider", "account_label", "pk").distinct())
    return ConnectionsOut(
        providers=_agent_provider_rows(agent),
        connections=[_out(c) for c in own] + [_out(c, granted_from=c.agent.slug) for c in granted],
    )


@agents_router.delete("/{slug}/connections/{connection_id}", response={204: None},
                      summary="Disconnect an agent's connection")
@gate("agent-admin", setting=True, step_up=True)
def disconnect_agent_connection(request: HttpRequest, slug: str, connection_id: int) -> Status:
    """Stop canopy holding this agent's connection and ask the provider to
    revoke it. From a browser this needs a fresh passkey check; agents sharing
    it lose access too."""
    agent = agent_for_admin(request, slug)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    conn = get_object_or_404(Connection, pk=connection_id, agent=agent)
    services.disconnect(conn, by=request.user)
    return Status(204, None)


def _admin_of_either(request: HttpRequest, slug: str, other_slug: str) -> tuple[Agent, bool]:
    """The owner agent `slug`, for a caller who administers it or `other_slug`,
    and whether the caller came in ONLY as the grantee's admin.

    The grantee's admin is confirmed first, then the owner is looked up without
    the read-scope pin: an agent shared across workspaces has a grantee admin
    who cannot read the owner, and must still be able to stop using it."""
    other = Agent.objects.filter(slug=other_slug).first()
    if other is not None and other.is_admin(request.user):
        owner = Agent.objects.filter(slug=slug).first()
        if owner is None:
            raise HttpError(404, "unknown agent")
        return owner, not owner.is_admin(request.user)
    agent = agent_for_read(request, slug)
    if agent.is_admin(request.user):
        return agent, False
    raise HttpError(403, "this action requires an admin of either agent")


def _grant_target(request: HttpRequest, slug: str, connection_id: int, other_slug: str, *, either: bool = False):
    grantee_only = False
    if either:
        agent, grantee_only = _admin_of_either(request, slug, other_slug)
    else:
        agent = agent_for_admin(request, slug)
    conn = get_object_or_404(Connection, pk=connection_id, agent=agent)
    other = Agent.objects.filter(slug=other_slug).first()
    if other is None:
        raise HttpError(404, "unknown agent")
    # The grantee's admin sees only what is shared with their agent: an owner's
    # connection that is not is "not found" to them, never "nothing to revoke".
    if grantee_only and not ConnectionGrant.objects.filter(connection=conn, agent=other).exists():
        raise HttpError(404, "not shared with that agent")
    return conn, other


@agents_router.put("/{slug}/connections/{connection_id}/grants/{other_slug}", response={204: None},
                   summary="Share an agent's connection with another agent")
@gate("agent-admin", setting=True, step_up=True)
def grant_connection(request: HttpRequest, slug: str, connection_id: int, other_slug: str) -> Status:
    """Let another agent act through this agent's connection. You must
    administer both agents. Sharing again is harmless."""
    conn, other = _grant_target(request, slug, connection_id, other_slug)
    # After the admin check: a non-admin gets 404/403, never a step-up prompt.
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    try:
        services.grant(conn, other, by=request.user)
    except PermissionDenied as exc:
        raise HttpError(403, str(exc)) from None
    except services.ConnectionError as exc:
        raise problem_for(exc, title=_title(conn.provider), owner=f"{slug}'s") from None
    return Status(204, None)


@agents_router.delete("/{slug}/connections/{connection_id}/grants/{other_slug}", response={204: None},
                      summary="Stop sharing an agent's connection with another agent")
@gate("agent-admin", setting=True, step_up=True)
def revoke_connection_grant(request: HttpRequest, slug: str, connection_id: int, other_slug: str) -> Status:
    """Take the other agent's access away. An admin of either the owning agent or
    the agent it was shared with may do this."""
    conn, other = _grant_target(request, slug, connection_id, other_slug, either=True)
    # After the admin check: a non-admin gets 404/403, never a step-up prompt.
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    g = (ConnectionGrant.objects.filter(connection=conn, agent=other)
         .select_related("connection__agent", "agent").first())
    if g is not None:
        try:
            services.revoke_grant(g, by=request.user)
        except PermissionDenied as exc:
            raise HttpError(403, str(exc)) from None
    return Status(204, None)


# ---- my CLI token ----------------------------------------------------------------

def _plain_pat(request: HttpRequest) -> bool:
    """A personal access token authenticated this request, over REST.

    An allowlist, not "not a browser": a token route must not answer whatever
    else is not a browser. An MCP tool call runs as the caller's PAT but puts the
    answer in a model's context; an OAuth-grant PAT is a connector's (Claude's)
    token, also a model's; a caller token speaks for a turn. All refused."""
    if getattr(request, "via_mcp", False) or getattr(request, "auth_method", None) != kinds.PAT:
        return False
    kind = kinds.kind_of(getattr(request, "auth_credential", None))
    return kind is not None and kind.name == kinds.PAT


def _token_problem(request: HttpRequest, exc: services.ConnectionError, p: type[providers.Provider],
                   workspace: str | None, conn: Connection | None) -> dict:
    """The extra members (and, where the case needs its own words, `detail`) of
    a 409 a person fixes themselves. `connect_url` is where to fix it: the start
    URL of the one workspace in question (the connection's, the one asked for,
    or the only one set up), else Settings, where they choose a workspace or
    disconnect a spare account. Several workspaces in play are listed in
    `workspaces`, and the detail says to pick one."""
    user, provider = request.user, p.name
    settings_url = request.build_absolute_uri("/settings/connections")
    out: dict = {}
    if exc.code not in ("not_connected", "reconnect_required", "ambiguous"):
        return out
    if conn is not None:                       # reconnect: the connection's own workspace
        out["connect_url"] = _start_url(request, provider, conn.app.workspace_id)
        return out
    pick = "pick one with `?workspace=<slug>` (`--workspace <slug>` in the CLI)"
    if exc.code == "ambiguous":
        rows = services.person_connections(user, provider, workspace=workspace)
        where = sorted({c.app.workspace_id for c in rows})
        out["accounts"] = [c.account_label for c in rows]
        out["workspaces"] = where
        out["connect_url"] = settings_url
        if len(where) > 1:
            out["detail"] = (f"Your {p.title} is connected in more than one workspace "
                             f"({', '.join(where)}); {pick}.")
        return out
    where = [a.workspace_id for a in services.apps_for_person(user, provider)
             if workspace is None or a.workspace_id == workspace]
    if len(where) == 1:
        out["connect_url"] = _start_url(request, provider, where[0])
        return out
    out["connect_url"] = settings_url
    if len(where) > 1:
        out["workspaces"] = where
        out["detail"] = (f"Your {p.title} is not connected. It is set up in more than one workspace "
                         f"({', '.join(where)}): connect in one, then {pick}.")
    return out


@me_router.post("/{provider}/token", response=AccessTokenOut,
                summary="A fresh access token for my own connection (CLI)")
@gate("self", kind="ephemeral-credential", step_up="a person's own token for their own CLI; PAT only")
def my_connection_token(request: HttpRequest, provider: str, refresh: bool = False,
                        workspace: str | None = None) -> AccessTokenOut:
    """Mint a short-lived access token for the account I connected, for my own
    command line. Needs a personal access token sent as a bearer: a browser
    session, an MCP call, an OAuth connector's token and a conversation's caller
    token are all refused (403 `token_route_needs_pat`), so neither a stolen
    cookie nor a model holding my token can pull a provider token. Only a
    connection made in a workspace I am still a member of counts; `?workspace=`
    picks one when I connected in several (409 `ambiguous` lists them in
    `workspaces`). 409 when I have not connected, or must reconnect
    (`connect_url`, an absolute URL, says where: a workspace's connect page, or
    Settings when I must first choose a workspace or disconnect a spare account). `?refresh=true` forces a new
    token even when the cached one looks fresh (for a CLI whose token the
    provider already rejected)."""
    if not _plain_pat(request):
        raise ProblemError(403, "A personal access token is required",
                           detail="Use the CLI (`canopy login`) to fetch your own token.",
                           members={"error": "token_route_needs_pat"})
    p = _provider_or_404(provider)
    # No outer transaction: a refresh rejection stamps the row, and that stamp
    # must survive the exception turned into a 409 below.
    conn = None
    try:
        conn = services.connection_for_person(request.user, provider, workspace=workspace or None)
        token = services.issue_token(conn, to="cli", by=request.user, refresh=refresh)
    except services.ConnectionError as exc:
        members = _token_problem(request, exc, p, workspace or None, conn)
        raise problem_for(exc, title=p.title, owner="Your", **members) from None
    return AccessTokenOut(access_token=token.token, instance_url=token.instance_url,
                          expires_at=token.expires_at, account_label=token.account_label)


# ---- a workspace's apps ----------------------------------------------------------

def _connection_count(app: ConnectionApp | None) -> int:
    if app is None:
        return 0
    # Annotated by the list (one query for every provider); a saved row counts its own.
    n = getattr(app, "n_connections", None)
    return app.connections.count() if n is None else n


def _app_out(request: HttpRequest, slug: str, cls: type[providers.Provider],
             app: ConnectionApp | None) -> ConnectionAppOut:
    # Never the secret: `has_secret` only.
    return ConnectionAppOut(
        provider=cls.name, title=cls.title,
        configured=app is not None and providers.for_app(app).is_configured(),
        client_id=app.client_id if app is not None else "",
        has_secret=bool(app is not None and app.client_secret_enc),
        options=dict(app.options) if app is not None else {},
        scopes=list(app.extra_scopes) if app is not None else [],
        scope_choices=[ScopeChoiceOut(scope=k, label=v) for k, v in cls.scope_choices.items()],
        option_fields=[OptionFieldOut(name=f.name, label=f.label, kind=f.kind, required=f.required,
                                      default=f.default, help=f.help) for f in cls.option_fields],
        callback_url=_callback_url(request, cls.name, slug),
        connection_count=_connection_count(app),
        updated_at=app.updated_at if app is not None else None,
        configured_by_email=((app.configured_by.email or app.configured_by.get_username())
                             if app is not None and app.configured_by is not None else ""),
    )


def _app_row(slug: str, provider: str) -> ConnectionApp | None:
    return (ConnectionApp.objects.select_related("configured_by")
            .filter(workspace_id=slug, provider=provider).first())


@workspaces_router.get("/{workspace}/connection-apps", response=ConnectionAppsOut,
                       summary="A workspace's connection apps")
@gate("integrations")
def list_connection_apps(request: HttpRequest, workspace: str) -> ConnectionAppsOut:
    """The OAuth client this workspace uses for each provider people and agents
    connect to (Salesforce, ...), set up or not: its client id, whether a secret
    is saved (never the secret), its settings and chosen scopes, the callback URL
    to register at the provider, and how many connections it holds. Workspace
    admins only."""
    ws = workspace_for(request.user, workspace, perms.INTEGRATIONS)
    rows = {a.provider: a for a in ConnectionApp.objects.select_related("configured_by").filter(workspace=ws)
            .annotate(n_connections=Count("connections"))}
    return ConnectionAppsOut(apps=[_app_out(request, ws.slug, c, rows.get(c.name))
                                   for c in providers.CLASSES.values()])


@workspaces_router.put("/{workspace}/connection-apps/{provider}", response=ConnectionAppOut,
                       summary="Set up or change a workspace's connection app")
@gate("integrations", setting=True, step_up=True)
def configure_connection_app(request: HttpRequest, workspace: str, provider: str,
                             payload: ConnectionAppIn) -> ConnectionAppOut:
    """Save this workspace's OAuth client for `provider`. A blank `client_secret`
    keeps the saved one; send a new one to rotate it (existing connections keep
    working). `options` merge over the saved settings; `scopes` replaces the
    chosen list. While accounts are connected with the app, its client id (and,
    for Salesforce, its login host) cannot change: 409 `app_locked`; remove the
    app to switch. 400 `invalid_app` says what was refused. From a browser this
    needs a fresh passkey check. Workspace admins only."""
    ws = workspace_for(request.user, workspace, perms.INTEGRATIONS)
    # After the permission check: a non-admin gets 404/403, never a step-up prompt.
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    cls = _provider_or_404(provider)
    try:
        app = services.configure_app(ws, cls.name, client_id=payload.client_id,
                                     client_secret=payload.client_secret, options=payload.options,
                                     scopes=payload.scopes, by=request.user)
    except services.ConnectionError as exc:
        raise problem_for(exc, title=cls.title, owner="This workspace's") from None
    return _app_out(request, ws.slug, cls, app)


@workspaces_router.delete("/{workspace}/connection-apps/{provider}", response={204: None},
                          summary="Remove a workspace's connection app")
@gate("integrations", setting=True, step_up=True)
def remove_connection_app(request: HttpRequest, workspace: str, provider: str) -> Status:
    """Remove this workspace's OAuth client for `provider`. Every account
    connected with it is disconnected (each revoked at the provider) — people's
    and agents'. Removing an app that is not set up is harmless. From a browser
    this needs a fresh passkey check. Workspace admins only."""
    ws = workspace_for(request.user, workspace, perms.INTEGRATIONS)
    # After the permission check: a non-admin gets 404/403, never a step-up prompt.
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    cls = _provider_or_404(provider)
    app = _app_row(ws.slug, cls.name)
    if app is not None:
        services.remove_app(app, by=request.user)
    return Status(204, None)
