"""The JSON half of connections: a workspace's apps, status and disconnect.

Errors from the service layer become responses in one place, `problem_for`.

Four routers, because the URLs live under four prefixes (see `apps/api/api.py`):
`router` at `/connections` (a person's own), `agents_router` at `/agents`
(an agent's, admin-only), `me_router` at `/me/connections` (a person's CLI; no
routes today) and
`workspaces_router` at `/workspaces` (a workspace's apps, its admins').
The browser legs of connecting are bare views (`views.py`).
"""
from __future__ import annotations

import uuid

from django.db.models import Count
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router, Status
from ninja.errors import HttpError

from apps.agents.gates import agent_for_admin
from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.passkeys import stepup
from apps.workspaces import permissions as perms
from apps.workspaces.gates import workspace_for

from . import providers, services, sharing
from . import recipients as rcpt
from .models import Connection, ConnectionApp
from .problems import problem_for, problem_for_wire
from .schemas import (
    ConnectionAppIn,
    ConnectionAppOut,
    ConnectionAppsOut,
    ConnectionOut,
    ConnectionsOut,
    ConnectionStatementOut,
    MyMachineOut,
    OptionFieldOut,
    ProviderOut,
    ScopeChoiceOut,
    ShareIn,
    ShareStateOut,
    WaitingOut,
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


def _missing_scopes(conn: Connection) -> list[ScopeChoiceOut]:
    # What the app asks for now that this grant lacks: an admin chose a scope after
    # it was connected. Labelled from the provider's menu (else the scope itself).
    cls = providers.CLASSES.get(conn.provider)
    if cls is None:
        return []
    return [ScopeChoiceOut(scope=sc, label=cls.scope_choices.get(sc, sc))
            for sc in providers.for_app(conn.app).missing_scopes(tuple(conn.scopes.split()))]


def _statement(conn: Connection) -> ConnectionStatementOut:
    # The statement names agents by pk (what endorsements sign); shown by slug.
    st = conn.statement or {}
    pks = [int(a) for a in st.get("agents", []) if str(a).isdigit()]
    slugs = dict(Agent.objects.filter(pk__in=pks).values_list("pk", "slug")) if pks else {}
    return ConnectionStatementOut(version=conn.statement_version, agents=sorted(slugs[p] for p in pks if p in slugs),
                                  my_computers=sorted(st.get("my_computers", [])))


def _holders(conn: Connection) -> list[str]:
    # The runners whose device keys the refresh envelope is sealed to now.
    return sorted({k.runner.name for k in rcpt.holders(conn)})


def _fingerprint(jkt: str) -> str:
    # As the browser shows a key (`vault/zk.ts` `fingerprint`): eight groups of four.
    s = "".join(c for c in jkt if c.isalnum()).upper()[:32]
    return " ".join(s[i:i + 4] for i in range(0, len(s), 4))


def _my_machines(conn: Connection) -> list[MyMachineOut]:
    jkts = (conn.statement or {}).get("my_computers") or []
    if conn.user_id is None or not jkts:
        return []
    return [MyMachineOut(jkt=k.ecdh_jkt, name=k.runner.name, fingerprint=_fingerprint(k.ecdh_jkt))
            for k in rcpt.my_machines(conn.user, jkts)]


def _out(conn: Connection, *, usable: bool, granted_from: str | None = None) -> ConnectionOut:
    return ConnectionOut(
        id=conn.pk, provider=conn.provider, title=_title(conn.provider),
        workspace=conn.app.workspace_id, workspace_name=conn.app.workspace.display_name,
        account_label=conn.account_label, instance_url=conn.instance_url, scopes=conn.scopes.split(),
        needs_reconnect=conn.needs_reconnect, usable=usable, missing_scopes=_missing_scopes(conn),
        state=conn.state, statement=_statement(conn), holders=_holders(conn),
        waiting=[WaitingOut(agent=w["agent"], runner=w["runner"]) for w in rcpt.waiting(conn)],
        my_machines=_my_machines(conn),
        connected_at=conn.created_at,
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
    shows as `needs_reconnect`, which the next real use of it keeps up to date;
    one in a workspace I have left is listed with `usable` false, to disconnect;
    one missing a scope its app now asks for lists it in `missing_scopes`."""
    rows = (services.connections_for(("user", request.user.pk))
            .select_related("connected_by", "app__workspace"))
    readable = services.readable_workspaces(request.user)
    return ConnectionsOut(providers=_person_provider_rows(request.user),
                          connections=[_out(c, usable=c.app.workspace_id in readable) for c in rows])


def _visible(request: HttpRequest, connection_id: uuid.UUID) -> Connection:
    # Mine, or an agent's I administer; anyone else's id is a 404 (never probed).
    conn = get_object_or_404(Connection.objects.select_related("app__workspace", "agent", "user"), pk=connection_id)
    if not sharing.visible(conn, request.user):
        raise HttpError(404, "Not Found")
    return conn


@router.get("/{connection_id}/share", response=ShareStateOut, summary="What sharing a connection needs")
@gate("self")
def get_share_state(request: HttpRequest, connection_id: uuid.UUID) -> ShareStateOut:
    """Everything the owner's browser needs to share a connection: its sealed
    envelopes (only their recipients can open them), the signed statement in
    force, the owner's key, every machine the owner could seal it to (each with
    the agent that admits it, or marked `mine`) and the endorsements to check
    them by. Only the person whose vault key it is sealed to: anyone else who can
    see it gets 403 `not_the_owner_key`; 409 `revoking` while it is being
    disconnected."""
    conn = _visible(request, connection_id)
    try:
        return ShareStateOut(**sharing.share_state(conn, request.user))
    except sharing.WireError as exc:
        raise problem_for_wire(exc) from None


@router.put("/{connection_id}/share", response=ConnectionOut, summary="Share a connection")
@gate("self", setting=True, step_up=True)
def share_connection(request: HttpRequest, connection_id: uuid.UUID, payload: ShareIn) -> ConnectionOut:
    """Store the owner's signed grant statement (which agents it is lent to, and
    whether it is used on their own computers) and the envelope their browser
    re-sealed to the machines it allows; the connection becomes `active`.
    canopy-web never opens the envelope: it checks the signer is the owner
    (403 `not_the_owner_key`), the signature (422 `bad_signature`), that the
    version moves forward (409 `statement_stale`), that every agent added is one
    the owner administers (403 `not_an_admin`) in the app's workspace (422
    `agent_outside_workspace`), that listed computers are the owner's own (422
    `not_your_machine`), that an agent's statement lists the agent (422
    `statement_missing_owner_agent`), that `expect` is what is stored (409
    `zk_value_changed`), that it is sealed only to the owner and allowed machines
    (422 `foreign_reader`) and that its writer MACs are the owner's (422
    `bad_writer_sig`). From a browser this needs a fresh passkey check."""
    conn = _visible(request, connection_id)
    # After the visibility check: someone who cannot see it gets 404, never a step-up prompt.
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    try:
        conn = sharing.share(conn, user=request.user, **payload.model_dump())
    except sharing.WireError as exc:
        raise problem_for_wire(exc) from None
    conn = (Connection.objects.select_related("connected_by", "app__workspace", "agent")
            .prefetch_related("grants__agent").get(pk=conn.pk))
    usable = (conn.app.workspace_id in services.readable_workspaces(request.user) if conn.user_id
              else services.agent_connection_usable(conn))
    return _out(conn, usable=usable)


@router.delete("/{connection_id}", response={204: None}, summary="Disconnect one of my connections")
@gate("self", setting=True)
def disconnect_mine(request: HttpRequest, connection_id: uuid.UUID) -> Status:
    """Stop canopy holding this connection.

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
    workspace has set up, which are what it connects with. A connection works
    only while its owning agent is in the workspace whose app made it: one made
    before the agent moved (or shared by an agent that has since moved) is listed
    with `usable` false. Only the agent's admins can see them."""
    agent = agent_for_admin(request, slug)
    own = (services.connections_for(("agent", agent.pk))
           .select_related("connected_by", "agent", "app__workspace")
           .prefetch_related("grants__agent"))
    granted = (Connection.objects.filter(grants__agent=agent)
               .select_related("connected_by", "agent", "app__workspace")
               .order_by("provider", "account_label", "pk").distinct())
    return ConnectionsOut(
        providers=_agent_provider_rows(agent),
        connections=([_out(c, usable=services.agent_connection_usable(c)) for c in own]
                     + [_out(c, usable=services.agent_connection_usable(c), granted_from=c.agent.slug)
                        for c in granted]),
    )


@agents_router.delete("/{slug}/connections/{connection_id}", response={204: None},
                      summary="Disconnect an agent's connection")
@gate("agent-admin", setting=True, step_up=True)
def disconnect_agent_connection(request: HttpRequest, slug: str, connection_id: uuid.UUID) -> Status:
    """Stop canopy holding this agent's connection. From a browser this needs a
    fresh passkey check; agents it is lent to lose access too."""
    agent = agent_for_admin(request, slug)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    conn = get_object_or_404(Connection, pk=connection_id, agent=agent)
    services.disconnect(conn, by=request.user)
    return Status(204, None)


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
        # Readable, not merely stored: one the key can no longer open is not a secret.
        has_secret=bool(app is not None and app.client_secret),
        options=dict(app.options) if app is not None else {},
        scopes=list(app.extra_scopes) if app is not None else [],
        scope_choices=[ScopeChoiceOut(scope=k, label=v) for k, v in cls.scope_choices.items()],
        option_fields=[OptionFieldOut(name=f.name, label=f.label, kind=f.kind, required=f.required,
                                      default=f.default, help=f.help) for f in cls.option_fields],
        locked_options=list(cls.locked_options),
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
    chosen list. While accounts are connected with the app, its client id and
    its `locked_options` (Salesforce: its login URL) cannot change: 409
    `app_locked`; remove the app to switch. Adding a scope does not change
    existing connections: each lists it in `missing_scopes` until reconnected.
    400 `invalid_app` says what was refused. From a browser this needs a fresh
    passkey check. Workspace admins only."""
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
    connected with it is disconnected — people's and agents' — and agents it was
    shared with lose it. Removing an app that is
    not set up is harmless. A workspace cannot be deleted while it has apps.
    From a browser this needs a fresh passkey check. Workspace admins only; not
    an MCP tool."""
    ws = workspace_for(request.user, workspace, perms.INTEGRATIONS)
    # After the permission check: a non-admin gets 404/403, never a step-up prompt.
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    cls = _provider_or_404(provider)
    app = _app_row(ws.slug, cls.name)
    if app is not None:
        services.remove_app(app, by=request.user)
    return Status(204, None)
