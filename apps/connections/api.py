"""The JSON half of connections: a workspace's apps, status and disconnect.

Errors from the service layer become responses in one place, `problem_for`.

Four routers, because the URLs live under four prefixes (see `apps/api/api.py`):
`router` at `/connections` (a person's own), `agents_router` at `/agents`
(an agent's, admin-only), `me_router` at `/me/connections` (a person's CLI on
their own enrolled computer: material, lease, write-back) and
`workspaces_router` at `/workspaces` (a workspace's apps, its admins').
A runner's material and write-back are `api_turns.py` and `api_runner.py`.
The browser legs of connecting are bare views (`views.py`).
"""
from __future__ import annotations

import uuid

from django.db.models import Count, Q
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router, Status
from ninja.errors import HttpError

from apps.agents.gates import agent_for_admin
from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.errors import ProblemError
from apps.api.route_gates import gate
from apps.harness.models import RunnerDeviceKey
from apps.passkeys import stepup
from apps.tokens import kinds
from apps.workspaces import permissions as perms
from apps.workspaces.gates import workspace_for

from . import proofs, providers, services, sharing, tokens
from . import recipients as rcpt
from .models import Connection, ConnectionApp
from .problems import problem_for, problem_for_wire
from .schemas import (
    ConnectionAppIn,
    ConnectionAppOut,
    ConnectionAppsOut,
    ConnectionMaterialOut,
    ConnectionOut,
    ConnectionsOut,
    ConnectionStatementOut,
    DisconnectOut,
    LeaseIn,
    LeaseOut,
    MyMachineOut,
    OptionFieldOut,
    ProviderOut,
    ReconnectRequiredIn,
    ScopeChoiceOut,
    ShareIn,
    ShareStateOut,
    UnreachableOut,
    WaitingOut,
    WriteBackIn,
    WriteBackOut,
)
from .wire import WireError

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


def _statement(conn: Connection, *, lent: bool = False) -> ConnectionStatementOut:
    # The statement names agents by pk (what endorsements sign); shown by slug. Which
    # computers its owner uses it on is not the business of a grantee's admins (`lent`).
    st = conn.statement or {}
    pks = [int(a) for a in st.get("agents", []) if str(a).isdigit()]
    slugs = dict(Agent.objects.filter(pk__in=pks).values_list("pk", "slug")) if pks else {}
    return ConnectionStatementOut(version=conn.statement_version, agents=sorted(slugs[p] for p in pks if p in slugs),
                                  my_computers=[] if lent else sorted(st.get("my_computers", [])),
                                  endorsers=sorted(st.get("endorsers", [])))


def _holders(conn: Connection, *, lent: bool = False) -> list[str]:
    # The runners whose device keys the refresh envelope is sealed to now. Which computers
    # its owner uses it on is not the business of a grantee's admins.
    hidden = set((conn.statement or {}).get("my_computers") or []) if lent else set()
    return sorted({k.runner.name for k in rcpt.holders(conn) if k.ecdh_jkt not in hidden})


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


def _lent_by(conn: Connection) -> str:
    # What an agent's list calls whoever lent it this: the agent's slug, or the person's name.
    if conn.agent_id is not None:
        return conn.agent.slug
    return conn.user.get_full_name() or conn.user.email or conn.user.get_username()


def _out(conn: Connection, *, usable: bool, granted_from: str | None = None, facts=None,
         replaced=None) -> ConnectionOut:
    lent = granted_from is not None
    pairs = rcpt.device_reasons(conn, facts=facts)
    return ConnectionOut(
        id=conn.pk, provider=conn.provider, title=_title(conn.provider),
        workspace=conn.app.workspace_id, workspace_name=conn.app.workspace.display_name,
        account_label=conn.account_label, instance_url=conn.instance_url, scopes=conn.scopes.split(),
        needs_reconnect=conn.needs_reconnect, usable=usable, missing_scopes=_missing_scopes(conn),
        state=conn.state, statement=_statement(conn, lent=lent), holders=_holders(conn, lent=lent),
        waiting=[WaitingOut(agent=w["agent"], runner=w["runner"]) for w in rcpt.waiting(conn, pairs=pairs)
                 if w["agent"] or not lent],
        owner_key_replaced=not lent and conn.pk in (replaced or ()),
        unreachable=[UnreachableOut(**u) for u in rcpt.unreachable(conn, pairs=pairs, facts=facts)],
        my_machines=[] if lent else _my_machines(conn),
        connected_at=conn.created_at,
        last_used_at=conn.last_used_at,
        connected_by_email=((conn.connected_by.email or conn.connected_by.get_username())
                            if conn.connected_by is not None else ""),
        granted_from=granted_from,
        # Agent-owned, not itself a shared row. Callers prefetch `grants__agent`.
        shared_with=(sorted(g.agent.slug for g in conn.grants.all())
                     if conn.agent_id and not lent else []),
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
    facts = rcpt.facts_for(rows)
    replaced = rcpt.owner_keys_replaced(rows)
    return ConnectionsOut(providers=_person_provider_rows(request.user),
                          connections=[_out(c, usable=c.app.workspace_id in readable, facts=facts, replaced=replaced)
                                       for c in rows])


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
    except WireError as exc:
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
    except WireError as exc:
        raise problem_for_wire(exc) from None
    conn = (Connection.objects.select_related("connected_by", "app__workspace", "agent")
            .prefetch_related("grants__agent").get(pk=conn.pk))
    usable = (conn.app.workspace_id in services.readable_workspaces(request.user) if conn.user_id
              else services.agent_connection_usable(conn))
    return _out(conn, usable=usable)


@router.delete("/{connection_id}", response=DisconnectOut, summary="Disconnect one of my connections")
@gate("self", setting=True)
def disconnect_mine(request: HttpRequest, connection_id: uuid.UUID) -> DisconnectOut:
    """Disconnect it: it leaves every list at once and is handed to no machine; a
    cloud box that holds it revokes it at the provider and it is then deleted
    (`outcome` `revoking`). With none to do that it is deleted at once (`deleted`).

    Only my own: someone else's id is a 404, not a 403, so ids cannot be probed."""
    conn = get_object_or_404(Connection.objects.exclude(state=Connection.REVOKING), pk=connection_id,
                             user=request.user)
    return DisconnectOut(outcome=services.disconnect(conn, by=request.user))


# ---- an agent's ------------------------------------------------------------------

@agents_router.get("/{slug}/connections", response=ConnectionsOut,
                   summary="An agent's connections, its own and shared with it")
@gate("agent-admin")
def agent_connections(request: HttpRequest, slug: str) -> ConnectionsOut:
    """What this agent can act as: connections it owns, and ones lent to it by
    another agent or a person (`granted_from` names the owner), and the providers
    its own workspace has set up, which are what it connects with. Its own works
    only while it is in the workspace whose app made it: one made before the agent
    moved is listed with `usable` false. A lent one works only while its grant is in
    force (`usable` false otherwise). Only the agent's admins can see them."""
    agent = agent_for_admin(request, slug)
    own = (services.connections_for(("agent", agent.pk))
           .select_related("connected_by", "agent", "app__workspace")
           .prefetch_related("grants__agent"))
    granted = (Connection.objects.filter(grants__agent=agent).exclude(state=Connection.REVOKING)
               .select_related("connected_by", "agent", "user", "app__workspace")
               .order_by("provider", "account_label", "pk").distinct())
    facts = rcpt.facts_for([*own, *granted])
    in_force = rcpt.lent_in_force(agent, granted)
    replaced = rcpt.owner_keys_replaced(own)
    return ConnectionsOut(
        providers=_agent_provider_rows(agent),
        connections=([_out(c, usable=services.agent_connection_usable(c), facts=facts, replaced=replaced)
                       for c in own]
                     + [_out(c, usable=c.pk in in_force, granted_from=_lent_by(c), facts=facts) for c in granted]),
    )


@agents_router.delete("/{slug}/connections/{connection_id}", response=DisconnectOut,
                      summary="Disconnect an agent's connection")
@gate("agent-admin", setting=True, step_up=True)
def disconnect_agent_connection(request: HttpRequest, slug: str, connection_id: uuid.UUID) -> DisconnectOut:
    """Stop this agent using this connection; agents it is lent to lose it too. `outcome`
    says whether a machine will revoke it or its copy was deleted at once, as for a
    person's. From a browser this needs a fresh passkey check."""
    agent = agent_for_admin(request, slug)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    conn = get_object_or_404(Connection.objects.exclude(state=Connection.REVOKING), pk=connection_id,
                             agent=agent)
    return DisconnectOut(outcome=services.disconnect(conn, by=request.user))


# ---- my CLI, on my own enrolled computer -------------------------------------------

def _plain_pat(request: HttpRequest) -> bool:
    """A personal access token authenticated this request, over REST.

    An allowlist, not "not a browser": a route handing out sealed credentials and
    an app secret must not answer whatever else is not a browser. An MCP tool call
    runs as the caller's PAT but puts the answer in a model's context; an
    OAuth-grant PAT is a connector's (Claude's) token, also a model's; a caller
    token speaks for a turn. All refused."""
    if getattr(request, "via_mcp", False) or getattr(request, "auth_method", None) != kinds.PAT:
        return False
    kind = kinds.kind_of(getattr(request, "auth_credential", None))
    return kind is not None and kind.name == kinds.PAT


def _my_machine(request: HttpRequest) -> RunnerDeviceKey:
    # A plain PAT, then a device proof by one of my own enrolled machines.
    if not _plain_pat(request):
        raise ProblemError(403, "A personal access token is required",
                           detail="Use the CLI (`canopy login`) on your enrolled computer.",
                           members={"error": "token_route_needs_pat"})
    try:
        return proofs.proving_machine(request, request.user)
    except WireError as exc:
        raise problem_for_wire(exc) from None


def _ambiguous_members(request: HttpRequest, p: type[providers.Provider], workspace: str | None) -> dict:
    where = sorted({c.app.workspace_id for c in services.person_connections(request.user, p.name,
                                                                             workspace=workspace)})
    out: dict = {"workspaces": where}
    if len(where) > 1:
        out["detail"] = (f"Your {p.title} is connected in more than one workspace ({', '.join(where)}); pick "
                         "one with `?workspace=<slug>` (`--workspace <slug>` in the CLI).")
    return out


@me_router.get("/{provider}/material", response=ConnectionMaterialOut,
               summary="My connection's sealed envelopes and app client, for my own computer (CLI)")
# a read: no write kind (the route-gate lint refuses one on a GET)
@gate("self", step_up="a person's own machine; PAT and device proof only")
def my_connection_material(request: HttpRequest, provider: str, workspace: str | None = None) -> ConnectionMaterialOut:
    """What my enrolled computer needs to use the account I connected: the sealed
    envelopes (only machines they are sealed to can open them), my signed grant
    statement, and the app's client id and secret to refresh with. Never a token.
    Each envelope's writer MAC is bound to the digest it replaces: for the refresh
    envelope that is `sealed_digest`, for the access envelope the SHA-256 (hex) of the
    stored `access_sealed` (empty string when there is none yet).

    Needs a personal access token over REST (403 `token_route_needs_pat` for a
    browser session, an MCP call, an OAuth connector's token or a caller token)
    and a device proof (`DPoP`) by one of my own enrolled computers (401
    `device_proof_required` without one; 403 `machine_not_enrolled` for any other
    key: enroll it with `canopy vault device --enroll`). The computer must be one
    the connection's `Use on my computers` lists (403 `not_on_my_computers`).
    `?workspace=` picks one when I connected in several (409 `ambiguous` lists them
    in `workspaces`). 409 `not_connected` when there is none (or it is not shared
    yet, or being disconnected), `needs_reconnect` when its refresh token is dead,
    and `waiting` when this computer may use it but holds no copy yet."""
    p = _provider_or_404(provider)
    key = _my_machine(request)
    try:
        conn = services.connection_for_person(request.user, p.name, workspace=workspace or None)
    except services.ConnectionError as exc:
        members = _ambiguous_members(request, p, workspace or None) if exc.code == "ambiguous" else {}
        detail = members.pop("detail", None)
        raise problem_for(exc, title=p.title, owner="Your", detail=detail, **members) from None
    try:
        return ConnectionMaterialOut(**tokens.material(conn, key))
    except WireError as exc:
        raise problem_for_wire(exc) from None


def _my_connection(request: HttpRequest, connection_id: uuid.UUID) -> tuple[Connection, RunnerDeviceKey]:
    # My own connection (anyone else's id is a 404), on one of my listed computers.
    key = _my_machine(request)
    conn = get_object_or_404(Connection.objects.select_related("app", "user"), pk=connection_id,
                             user=request.user)
    try:
        tokens.admits_my_computer(conn, key)
    except WireError as exc:
        raise problem_for_wire(exc) from None
    return conn, key


@me_router.post("/{connection_id}/refresh-lease", response=LeaseOut,
                summary="Take the refresh lease on my connection (CLI)")
@gate("self", kind="ephemeral-credential", step_up="a person's own machine; PAT and device proof only")
def take_my_refresh_lease(request: HttpRequest, connection_id: uuid.UUID, payload: LeaseIn) -> LeaseOut:
    """Take the one refresh lease (60 s; send `lease_id` to renew the one I hold)
    before refreshing my connection at the provider on my own computer. 409
    `lease_held` with `retry_after` while another machine holds it: wait for its new
    access token instead of refreshing too. Renew on `ttl_seconds`, not
    `expires_at`. PAT and device proof as for the material."""
    conn, key = _my_connection(request, connection_id)
    try:
        return LeaseOut(**tokens.take_lease(conn, key, payload.lease_id))
    except WireError as exc:
        raise problem_for_wire(exc) from None


@me_router.delete("/{connection_id}/refresh-lease/{lease_id}", response={204: None},
                  summary="Give up the refresh lease on my connection (CLI)")
@gate("self", kind="ephemeral-credential", step_up="a person's own machine; PAT and device proof only")
def release_my_refresh_lease(request: HttpRequest, connection_id: uuid.UUID, lease_id: uuid.UUID) -> Status:
    """Release the refresh lease this computer holds; harmless when it holds none."""
    conn, key = _my_connection(request, connection_id)
    tokens.release_lease(conn, key, lease_id)
    return Status(204, None)


@me_router.put("/{connection_id}/sealed", response=WriteBackOut,
               summary="Write back my connection's refreshed envelopes (CLI)")
@gate("self", kind="ephemeral-credential", step_up="a person's own machine; PAT and device proof only")
def write_back_my_connection(request: HttpRequest, connection_id: uuid.UUID, payload: WriteBackIn) -> WriteBackOut:
    """Store the access token my computer just minted, sealed to exactly who may open
    the connection now (and the refresh token, when it rotated), against the
    `sealed_digest` read. 409 `lease_lost`, `zk_value_changed`, `revoking`; 422
    `foreign_reader`, `zk_rewrap_required`, `bad_writer_sig`. Only after this
    succeeds is the token used."""
    conn, key = _my_connection(request, connection_id)
    try:
        digest = tokens.write_back(conn, device_key=key, **payload.model_dump())
    except WireError as exc:
        raise problem_for_wire(exc) from None
    return WriteBackOut(sealed_digest=digest)


@me_router.post("/{connection_id}/reconnect-required", response={204: None},
                summary="Report my connection's refresh refused (CLI)")
@gate("self", kind="ephemeral-credential", step_up="a person's own machine; PAT and device proof only")
def my_connection_reconnect_required(request: HttpRequest, connection_id: uuid.UUID,
                                     payload: ReconnectRequiredIn) -> Status:
    """The provider refused the refresh made under `lease_id`. `invalid_grant` marks
    the connection `needs_reconnect` (connect it again) and ends the lease; any other error is
    only noted. `detail` is recorded with it. 409 `lease_lost` unless this computer holds the lease."""
    conn, key = _my_connection(request, connection_id)
    try:
        tokens.mark_reconnect_required(conn, key, payload.lease_id, payload.error, payload.detail)
    except WireError as exc:
        raise problem_for_wire(exc) from None
    return Status(204, None)


# ---- a workspace's apps ----------------------------------------------------------

def _connection_count(app: ConnectionApp | None) -> int:
    if app is None:
        return 0
    # Annotated by the list (one query for every provider); a saved row counts its own.
    n = getattr(app, "n_connections", None)
    return app.connections.exclude(state=Connection.REVOKING).count() if n is None else n


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
    # A removed app is hidden at once; it lingers only for its revoking rows' holders.
    live = ~Q(connections__state=Connection.REVOKING)
    rows = {a.provider: a for a in ConnectionApp.objects.select_related("configured_by")
            .filter(workspace=ws, removed_at__isnull=True)
            .annotate(n_connections=Count("connections", filter=live))}
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
    """Remove this workspace's OAuth client for `provider`: hidden at once. Every
    account connected with it is disconnected — people's and agents' — and agents
    it was shared with lose it; the app is deleted once the machines holding them
    have revoked them (or seven days pass). Removing an app that is
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
