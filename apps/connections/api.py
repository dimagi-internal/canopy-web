"""The JSON half of connections: status, disconnect, sharing, and my CLI token.

Three routers, because the URLs live under three prefixes (see `apps/api/api.py`):
`router` at `/connections` (a person's own), `agents_router` at `/agents`
(an agent's, admin-only) and `me_router` at `/me/connections` (the CLI token).
The browser legs of connecting are bare views (`views.py`).
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied
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

from . import providers, services
from .models import Connection, ConnectionGrant
from .schemas import AccessTokenOut, ConnectionOut, ConnectionsOut, ProviderOut

router = Router(auth=session_auth, tags=["connections"])
agents_router = Router(auth=session_auth, tags=["connections"])
me_router = Router(auth=session_auth, tags=["connections"])


def _provider_rows() -> list[ProviderOut]:
    return [ProviderOut(name=p.name, title=p.title, configured=p.is_configured())
            for p in providers.ALL.values()]


def _out(conn: Connection, *, granted_from: str | None = None) -> ConnectionOut:
    return ConnectionOut(
        id=conn.pk, provider=conn.provider, title=providers.get(conn.provider).title,
        account_label=conn.account_label, instance_url=conn.instance_url,
        needs_reconnect=conn.needs_reconnect, connected_at=conn.created_at,
        last_used_at=conn.last_used_at,
        connected_by_email=conn.connected_by.email or conn.connected_by.get_username(),
        granted_from=granted_from,
        # Agent-owned, not itself a shared row. Callers prefetch `grants__agent`.
        shared_with=(sorted(g.agent.slug for g in conn.grants.all())
                     if conn.agent_id and granted_from is None else []),
    )


def _connect_url(request: HttpRequest, provider: str) -> str:
    # Absolute: the CLI prints it for a person to open, far from any page.
    return request.build_absolute_uri(reverse("connections_start", args=[provider]))


def _check_provider(provider: str) -> None:
    try:
        providers.get(provider)
    except providers.UnknownProvider:
        raise HttpError(404, "unknown provider") from None


# ---- a person's own ------------------------------------------------------------

@router.get("/", response=ConnectionsOut, summary="My connections")
@gate("self")
def my_connections(request: HttpRequest) -> ConnectionsOut:
    """The services canopy can connect to, and the accounts I have connected.

    Never calls the provider, so it is cheap to read on every visit to Settings.
    Whether a grant still works shows as `needs_reconnect`, which the next real
    use of it keeps up to date."""
    rows = services.connections_for(("user", request.user.pk)).select_related("connected_by")
    return ConnectionsOut(providers=_provider_rows(), connections=[_out(c) for c in rows])


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
    shares with it (`granted_from` names the owner). Only the agent's admins can see them."""
    agent = agent_for_admin(request, slug)
    own = services.connections_for(("agent", agent.pk)).select_related("connected_by").prefetch_related("grants__agent")
    granted = (Connection.objects.filter(grants__agent=agent).select_related("connected_by", "agent")
               .order_by("provider", "account_label", "pk").distinct())
    return ConnectionsOut(
        providers=_provider_rows(),
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


def _admin_of_either(request: HttpRequest, slug: str, other_slug: str) -> Agent:
    """The owner agent `slug`, for a caller who administers it or `other_slug`."""
    agent = agent_for_read(request, slug)
    if agent.is_admin(request.user):
        return agent
    other = Agent.objects.filter(slug=other_slug).first()
    if other is not None and other.is_admin(request.user):
        return agent
    raise HttpError(403, "this action requires an admin of either agent")


def _grant_target(request: HttpRequest, slug: str, connection_id: int, other_slug: str, *, either: bool = False):
    agent = _admin_of_either(request, slug, other_slug) if either else agent_for_admin(request, slug)
    conn = get_object_or_404(Connection, pk=connection_id, agent=agent)
    other = Agent.objects.filter(slug=other_slug).first()
    if other is None:
        raise HttpError(404, "unknown agent")
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
        raise ProblemError(409, "Cannot share this connection", detail=str(exc),
                           members={"error": exc.code}) from None
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


@me_router.post("/{provider}/token", response=AccessTokenOut,
                summary="A fresh access token for my own connection (CLI)")
@gate("self", kind="ephemeral-credential", step_up="a person's own token for their own CLI; PAT only")
def my_connection_token(request: HttpRequest, provider: str, refresh: bool = False) -> AccessTokenOut:
    """Mint a short-lived access token for the account I connected, for my own
    command line. Needs a personal access token sent as a bearer: a browser
    session, an MCP call, an OAuth connector's token and a conversation's caller
    token are all refused (403 `token_route_needs_pat`), so neither a stolen
    cookie nor a model holding my token can pull a Salesforce token. 409 when I
    have not connected, or must reconnect (`connect_url`, an absolute URL, says
    where). `?refresh=true` forces a new token even when the cached one looks
    fresh (for a CLI whose token Salesforce already rejected)."""
    if not _plain_pat(request):
        raise ProblemError(403, "A personal access token is required",
                           detail="Use the CLI (`canopy login`) to fetch your own token.",
                           members={"error": "token_route_needs_pat"})
    _check_provider(provider)
    url = _connect_url(request, provider)
    rows = list(services.connections_for(("user", request.user.pk)).filter(provider=provider))
    if not rows:
        raise ProblemError(409, "Not connected", detail=f"You have not connected {provider}.",
                           members={"error": "not_connected", "connect_url": url})
    if len(rows) > 1:
        raise ProblemError(409, "More than one account connected",
                           detail="Disconnect all but one; this route returns a single token.",
                           members={"error": "ambiguous", "connect_url": url,
                                    "accounts": [c.account_label for c in rows]})
    # No outer transaction: a refresh rejection stamps the row, and that stamp
    # must survive the exception we turn into a 409 below.
    try:
        token = services.access_token_for(rows[0], refresh=refresh)
    except services.NotConfigured:
        raise ProblemError(409, "Not set up", detail=f"{provider} is not set up on this deployment.",
                           members={"error": "not_configured"}) from None
    except services.ReconnectRequired:
        raise ProblemError(409, "Reconnect required", detail=f"Your {provider} connection no longer works.",
                           members={"error": "reconnect_required", "connect_url": url}) from None
    except services.ConnectionError as exc:
        if exc.code == "provider_unavailable":
            raise ProblemError(503, "Provider unavailable", detail=f"{provider} could not be reached.") from None
        raise ProblemError(502, "Provider error", detail=f"{provider} refused the request.") from None
    return AccessTokenOut(access_token=token.token, instance_url=token.instance_url,
                          expires_at=token.expires_at, account_label=token.account_label)
