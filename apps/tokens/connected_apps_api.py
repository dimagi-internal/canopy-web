"""`/api/workspaces/{slug}/connected-apps` — connecting a site to canopy.

Mounted on the workspaces prefix rather than under `/api/tokens` because this
is a tenant-admin surface and shares its shape with the others there (members,
invites, the shared vault): `{slug}` in the path, owner-only, 404 before 403 so
a non-member cannot probe which workspaces exist.

The domain rules live in `embed_apps.py`, not here. This module is the HTTP
edge — statuses, serialisation and the one thing an endpoint must own, which is
that the raw secret is returned exactly once, on the response that creates it.
"""

from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Schema, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth

from . import embed_apps
from .audit import record as audit
from .models import AppCredential, EmbedAuditLog

connected_apps_router = Router(auth=session_auth, tags=["connected apps"])

#: `bad_*` is the caller's input; `not_owner` is authorisation; a name clash is
#: a conflict. Mapped once so a new refusal cannot pick its own status.
_STATUS = {
    "not_found": 404,
    "not_owner": 403,
    "not_configured": 409,
    "duplicate_name": 409,
    "bad_name": 422,
    "bad_origin": 422,
    "bad_key": 422,
    "bad_jwks_url": 422,
    "already_shown": 409,
    "unknown_agent": 422,
}


class ConnectedAgentOut(Schema):
    slug: str
    name: str


class ConnectedAppOut(Schema):
    """A site connected to canopy.

    Carries no secret. The raw credential exists only in the response that
    minted it — there is nothing to re-read here, by construction.
    """

    id: int
    name: str
    origins: list[str]
    #: Domains whose EXISTING canopy users this site's visitors arrive as
    #: (who-is-asking §2). Empty: every visitor is a contact.
    resolvable_domains: list[str] = []
    agents: list[ConnectedAgentOut]
    #: Registered PEM public keys. Returned in full — they are public by
    #: definition, and showing only a count would leave an operator unable to
    #: tell which key they are about to retire.
    public_keys: list[str]
    signs_assertions: bool
    #: Where the site publishes its keys, if it does. `signs_assertions` is
    #: true for either door — a published JWKS or a pasted key.
    jwks_url: str
    #: False when another workspace registered this site and this one merely
    #: grants it — the page uses it to show what is editable here.
    administered_here: bool = True
    #: Every tenant this site serves, so an owner can see it is shared. Slugs
    #: only: which agents those tenants offer is their business.
    agent_workspaces: list[str] = []
    shows_on_canopy_pages: bool
    created_at: str
    last_used_at: str | None
    revoked: bool


class ConnectedAppCreatedOut(Schema):
    app: ConnectedAppOut
    #: Shown once. canopy stores only a hash, so this cannot be recovered.
    secret: str


class SecretOut(Schema):
    secret: str


class ConnectIn(Schema):
    name: str
    origins: list[str] = []
    agents: list[str] = []
    public_keys: list[str] = []
    jwks_url: str = ""
    resolvable_domains: list[str] = []
    show_on_canopy_pages: bool = False


class UpdateIn(Schema):
    origins: list[str] | None = None
    jwks_url: str | None = None
    agents: list[str] | None = None
    public_keys: list[str] | None = None
    show_on_canopy_pages: bool | None = None
    resolvable_domains: list[str] | None = None


def _out(app: AppCredential, workspace_slug: str = "") -> ConnectedAppOut:
    return ConnectedAppOut(
        id=app.pk,
        name=app.name,
        # The SANITISED list, matching what the browser will actually be sent.
        # Echoing the raw column would show a rejected origin as though it were
        # in force, which is the confusion `frame_origins()` exists to prevent.
        origins=app.frame_origins(),
        # THIS tenant's grant, not a site-wide setting: what another tenant
        # allows this site to resolve is that tenant's business.
        resolvable_domains=list(_grant_domains(app, workspace_slug)),
        administered_here=(app.workspace_id == workspace_slug) if workspace_slug else True,
        agent_workspaces=sorted({link.agent.workspace_id for link in app.allowed_agents.all()}),
        public_keys=list(app.public_keys or []),
        jwks_url=app.jwks_url or "",
        signs_assertions=bool(app.public_keys) or bool(app.jwks_url),
        # Only the agents THIS tenant granted. Another tenant's agents through
        # the same site are not this page's business, and listing them would
        # leak the existence of that tenant's agents to an unrelated owner.
        agents=[
            ConnectedAgentOut(slug=link.agent.slug, name=link.agent.name)
            for link in app.allowed_agents.all()
            if not workspace_slug or link.agent.workspace_id == workspace_slug
        ],
        shows_on_canopy_pages=app.show_on_canopy_pages,
        created_at=app.created_at.isoformat(),
        last_used_at=app.last_used_at.isoformat() if app.last_used_at else None,
        revoked=app.revoked_at is not None,
    )


def _grant_domains(app: AppCredential, workspace_slug: str) -> list[str]:
    for grant in app.tenant_grants.all():
        if not workspace_slug or grant.workspace_id == workspace_slug:
            return list(grant.resolvable_domains or [])
    return []


def _own_origin(request: HttpRequest) -> str:
    """canopy's own origin, as this request saw it.

    Scheme + host + port and no path, which is exactly what `frame-ancestors`
    takes. From the request rather than a form field because it is the address
    the person is looking at — the one value that cannot be typed wrong.
    """
    return f"{request.scheme}://{request.get_host()}"


def _refuse(exc: embed_apps.EmbedAppError):
    return HttpError(_STATUS.get(exc.code, 400), f"{exc.code}: {exc.message}")


def _app_or_404(request: HttpRequest, slug: str, app_id: int) -> AppCredential:
    try:
        app = embed_apps.apps_for(request.user, slug).filter(pk=app_id).first()
    except embed_apps.EmbedAppError as exc:
        raise _refuse(exc)
    if app is None:
        raise HttpError(404, "no such connected app in this workspace")
    return app


@connected_apps_router.get("/{slug}/connected-apps", response=list[ConnectedAppOut],
                           summary="Sites connected to this workspace")
def list_connected_apps(request: HttpRequest, slug: str) -> list[ConnectedAppOut]:
    """Every site this workspace has connected, with what each may do."""
    try:
        return [_out(a, slug) for a in embed_apps.apps_for(request.user, slug)]
    except embed_apps.EmbedAppError as exc:
        raise _refuse(exc)


@connected_apps_router.post("/{slug}/connected-apps", response={201: ConnectedAppCreatedOut},
                            summary="Connect a site")
def connect_app(request: HttpRequest, slug: str, payload: ConnectIn) -> Status:
    """Register a site, and return its secret once."""
    try:
        raw, app = embed_apps.register(
            user=request.user, workspace_slug=slug, name=payload.name,
            origins=payload.origins, agents=payload.agents,
            public_keys=payload.public_keys, jwks_url=payload.jwks_url,
            resolvable_domains=payload.resolvable_domains or None,
        )
        if payload.show_on_canopy_pages:
            embed_apps.set_show_on_canopy_pages(
                app=app, on=True, origin=_own_origin(request)
            )
    except embed_apps.EmbedAppError as exc:
        audit(event=EmbedAuditLog.CONNECT, request=request, app_name=payload.name,
              actor=request.user, ok=False, reason=exc.code)
        raise _refuse(exc)
    audit(event=EmbedAuditLog.CONNECT, request=request, app=app, actor=request.user,
          detail=f"origins={app.frame_origins()} agents={payload.agents}")
    return Status(201, ConnectedAppCreatedOut(app=_out(app, slug), secret=raw))


@connected_apps_router.patch("/{slug}/connected-apps/{int:app_id}", response=ConnectedAppOut,
                             summary="Change what a connected site may do")
def update_connected_app(request: HttpRequest, slug: str, app_id: int,
                         payload: UpdateIn) -> ConnectedAppOut:
    app = _app_or_404(request, slug, app_id)
    try:
        embed_apps.update(
            user=request.user, app=app, workspace_slug=slug, origins=payload.origins,
            agents=payload.agents, public_keys=payload.public_keys,
            resolvable_domains=payload.resolvable_domains, jwks_url=payload.jwks_url,
        )
        if payload.show_on_canopy_pages is not None:
            embed_apps.set_show_on_canopy_pages(
                app=app, on=payload.show_on_canopy_pages, origin=_own_origin(request)
            )
    except embed_apps.EmbedAppError as exc:
        audit(event=EmbedAuditLog.UPDATE, request=request, app=app, actor=request.user,
              ok=False, reason=exc.code)
        raise _refuse(exc)
    app.refresh_from_db()
    # What it is NOW, not what was asked for: a partial payload leaves the rest
    # untouched, and the trail has to say what the app can actually do.
    audit(event=EmbedAuditLog.UPDATE, request=request, app=app, actor=request.user,
          detail=f"origins={app.frame_origins()} resolvable={_grant_domains(app, slug)} "
                 f"agents={[l.agent.slug for l in app.allowed_agents.all()]}")
    return _out(app, slug)


@connected_apps_router.post("/{slug}/connected-apps/{int:app_id}/rotate", response=SecretOut,
                            summary="Issue a new secret, invalidating the old one")
def rotate_secret(request: HttpRequest, slug: str, app_id: int) -> SecretOut:
    """The previous secret stops working immediately — that is the point of the
    button, since it is reached for when the old one has leaked."""
    app = _app_or_404(request, slug, app_id)
    secret = embed_apps.rotate(app)
    audit(event=EmbedAuditLog.ROTATE, request=request, app=app, actor=request.user)
    return SecretOut(secret=secret)


@connected_apps_router.delete("/{slug}/connected-apps/{int:app_id}", response={204: None},
                              summary="Disconnect a site")
def disconnect_app(request: HttpRequest, slug: str, app_id: int) -> Status:
    """THIS workspace stops using the site. Every other tenant is unaffected.

    Withdrawn rather than deleted: the grant is the audit trail of what this
    workspace once allowed. The site itself is retired only when the last
    tenant leaves, at which point retiring it takes nothing from anybody.
    """
    app = _app_or_404(request, slug, app_id)
    try:
        embed_apps.disconnect(user=request.user, app=app, workspace_slug=slug)
    except embed_apps.EmbedAppError as exc:
        raise _refuse(exc)
    audit(event=EmbedAuditLog.DISCONNECT, request=request, app=app, actor=request.user,
          detail=f"withdrawn by {slug}")
    return Status(204, None)


# --- a site another workspace registered, granted by this one -----------------
#
# Withdrawing is `DELETE /connected-apps/{id}` — the same "we stop" as for a
# site this workspace registered, because from a tenant's side they are one
# act. There is deliberately no second endpoint for it.


class GrantIn(Schema):
    """Which site, named the way a host names it."""

    name: str
    resolvable_domains: list[str] = []
    agents: list[str] = []


@connected_apps_router.post("/{slug}/connected-apps/grants", response={201: ConnectedAppOut},
                            summary="Let a site another workspace registered act for this one")
def grant_site(request: HttpRequest, slug: str, payload: GrantIn) -> Status:
    """Authorize an already-registered site to act for this workspace.

    A site is one identity in the world and may serve several tenants; this is
    how the second and every later tenant says yes, without touching the site's
    keys, its origins, or any other tenant's grant. The site must already
    exist — registering one is a different act, and typing a name that happens
    to be free would silently create an identity nobody controls.
    """
    app = AppCredential.objects.filter(name=(payload.name or "").strip(),
                                       revoked_at__isnull=True).first()
    if app is None:
        # Same answer as a name that exists but is revoked: a site's name is not
        # a secret, but distinguishing the two tells a prober which of their
        # guesses used to be real.
        raise HttpError(404, f"no connected site named {(payload.name or '').strip()!r}")
    try:
        embed_apps.authorize_tenant(user=request.user, app=app, workspace_slug=slug,
                                    resolvable_domains=payload.resolvable_domains)
        if payload.agents:
            embed_apps.set_agents(app, slug, payload.agents)
    except embed_apps.EmbedAppError as exc:
        audit(event=EmbedAuditLog.UPDATE, request=request, app=app, actor=request.user,
              ok=False, reason=exc.code)
        raise _refuse(exc)
    app.refresh_from_db()
    audit(event=EmbedAuditLog.UPDATE, request=request, app=app, actor=request.user,
          detail=f"granted to {slug} agents={payload.agents}")
    return Status(201, _out(app, slug))


