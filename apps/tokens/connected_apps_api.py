"""`/api/workspaces/{slug}/connected-apps` — connecting a site to canopy.

Mounted on the workspaces prefix rather than under `/api/tokens` because this
is a tenant-admin surface and shares its shape with the others there (members,
invites, the shared vault): `{slug}` in the path, owner-only, 404 before 403 so
a non-member cannot probe which workspaces exist.

The domain rules live in `embed_apps.py`, not here. This module is the HTTP
edge — statuses and serialisation.
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

    A site has no secret: it proves itself by signing (`jwks_url` or
    `public_keys`), so there is nothing here to protect or rotate.
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
    shows_on_canopy_pages: bool
    created_at: str
    last_used_at: str | None
    revoked: bool


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


def _out(app: AppCredential) -> ConnectedAppOut:
    return ConnectedAppOut(
        id=app.pk,
        name=app.name,
        # The SANITISED list, matching what the browser will actually be sent.
        # Echoing the raw column would show a rejected origin as though it were
        # in force, which is the confusion `frame_origins()` exists to prevent.
        origins=app.frame_origins(),
        resolvable_domains=list(app.resolvable_domains or []),
        public_keys=list(app.public_keys or []),
        jwks_url=app.jwks_url or "",
        signs_assertions=bool(app.public_keys) or bool(app.jwks_url),
        agents=[
            ConnectedAgentOut(slug=link.agent.slug, name=link.agent.name)
            for link in app.allowed_agents.all()
        ],
        shows_on_canopy_pages=app.show_on_canopy_pages,
        created_at=app.created_at.isoformat(),
        last_used_at=app.last_used_at.isoformat() if app.last_used_at else None,
        revoked=app.revoked_at is not None,
    )


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
        return [_out(a) for a in embed_apps.apps_for(request.user, slug)]
    except embed_apps.EmbedAppError as exc:
        raise _refuse(exc)


@connected_apps_router.post("/{slug}/connected-apps", response={201: ConnectedAppOut},
                            summary="Connect a site")
def connect_app(request: HttpRequest, slug: str, payload: ConnectIn) -> Status:
    """Register a site in this workspace."""
    try:
        app = embed_apps.register(
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
    return Status(201, _out(app))


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
          detail=f"origins={app.frame_origins()} resolvable={app.resolvable_domains} "
                 f"agents={[l.agent.slug for l in app.allowed_agents.all()]}")
    return _out(app)


@connected_apps_router.delete("/{slug}/connected-apps/{int:app_id}", response={204: None},
                              summary="Disconnect a site")
def disconnect_app(request: HttpRequest, slug: str, app_id: int) -> Status:
    """This workspace stops using the site. It stops verifying immediately.

    Retired rather than deleted: the row is what this workspace's visitors'
    records hang off, and the audit trail of what it once allowed.
    """
    app = _app_or_404(request, slug, app_id)
    try:
        embed_apps.disconnect(user=request.user, app=app, workspace_slug=slug)
    except embed_apps.EmbedAppError as exc:
        raise _refuse(exc)
    audit(event=EmbedAuditLog.DISCONNECT, request=request, app=app, actor=request.user,
          detail=f"disconnected by {slug}")
    return Status(204, None)
