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
    "bad_domain": 422,
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
    delegation_domains: list[str]
    agents: list[ConnectedAgentOut]
    is_self: bool
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
    delegation_domains: list[str] = []
    agents: list[str] = []


class UpdateIn(Schema):
    origins: list[str] | None = None
    delegation_domains: list[str] | None = None
    agents: list[str] | None = None


class EnableSelfIn(Schema):
    agents: list[str] = []


def _out(app: AppCredential) -> ConnectedAppOut:
    return ConnectedAppOut(
        id=app.pk,
        name=app.name,
        # The SANITISED list, matching what the browser will actually be sent.
        # Echoing the raw column would show a rejected origin as though it were
        # in force, which is the confusion `frame_origins()` exists to prevent.
        origins=app.frame_origins(),
        delegation_domains=list(app.allowed_delegation_domains or []),
        agents=[
            ConnectedAgentOut(slug=link.agent.slug, name=link.agent.name)
            for link in app.allowed_agents.all()
        ],
        is_self=app.name == embed_apps.self_app_name(),
        created_at=app.created_at.isoformat(),
        last_used_at=app.last_used_at.isoformat() if app.last_used_at else None,
        revoked=app.revoked_at is not None,
    )


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


@connected_apps_router.post("/{slug}/connected-apps", response={201: ConnectedAppCreatedOut},
                            summary="Connect a site")
def connect_app(request: HttpRequest, slug: str, payload: ConnectIn) -> Status:
    """Register a site, and return its secret once."""
    try:
        raw, app = embed_apps.register(
            user=request.user, workspace_slug=slug, name=payload.name,
            origins=payload.origins, domains=payload.delegation_domains,
            agents=payload.agents,
        )
    except embed_apps.EmbedAppError as exc:
        audit(event=EmbedAuditLog.CONNECT, request=request, app_name=payload.name,
              actor=request.user, ok=False, reason=exc.code)
        raise _refuse(exc)
    audit(event=EmbedAuditLog.CONNECT, request=request, app=app, actor=request.user,
          detail=f"origins={app.frame_origins()} domains={app.allowed_delegation_domains} "
                 f"agents={payload.agents}")
    return Status(201, ConnectedAppCreatedOut(app=_out(app), secret=raw))


# Registered BEFORE the `{app_id}` routes on purpose. Django matches URL
# patterns in order, and `enable-self` sits exactly where an id goes — with the
# id route first, the button 405s and reads as a missing feature. (Ninja does
# not narrow `app_id: int` to a numeric path converter, so the types do not
# separate them for us; a test asserts the POST reaches this view.)
@connected_apps_router.post("/{slug}/connected-apps/enable-self",
                            response=ConnectedAppOut,
                            summary="Turn on canopy's widget on canopy's own pages")
def enable_self_widget(request: HttpRequest, slug: str, payload: EnableSelfIn) -> ConnectedAppOut:
    """One act, because every input is a fact about canopy rather than a choice.

    The origin is taken from THIS request, not from the body. It is the only
    value that is certainly right — it is the address the person is looking at —
    and taking it from the caller would let a form typo produce a connection
    that silently never frames.
    """
    # Scheme + host + port, with no path: exactly what `frame-ancestors` takes.
    origin = f"{request.scheme}://{request.get_host()}"
    try:
        _raw, app = embed_apps.enable_self(
            user=request.user, workspace_slug=slug, origin=origin, agents=payload.agents,
        )
    except embed_apps.EmbedAppError as exc:
        audit(event=EmbedAuditLog.CONNECT, request=request, actor=request.user,
              ok=False, reason=exc.code, detail="enable-self")
        raise _refuse(exc)
    audit(event=EmbedAuditLog.CONNECT, request=request, app=app, actor=request.user,
          detail=f"enable-self origin={origin} agents={payload.agents}")
    return _out(app)


@connected_apps_router.patch("/{slug}/connected-apps/{app_id}", response=ConnectedAppOut,
                             summary="Change what a connected site may do")
def update_connected_app(request: HttpRequest, slug: str, app_id: int,
                         payload: UpdateIn) -> ConnectedAppOut:
    app = _app_or_404(request, slug, app_id)
    try:
        embed_apps.update(
            user=request.user, app=app, origins=payload.origins,
            domains=payload.delegation_domains, agents=payload.agents,
        )
    except embed_apps.EmbedAppError as exc:
        audit(event=EmbedAuditLog.UPDATE, request=request, app=app, actor=request.user,
              ok=False, reason=exc.code)
        raise _refuse(exc)
    app.refresh_from_db()
    # What it is NOW, not what was asked for: a partial payload leaves the rest
    # untouched, and the trail has to say what the app can actually do.
    audit(event=EmbedAuditLog.UPDATE, request=request, app=app, actor=request.user,
          detail=f"origins={app.frame_origins()} domains={app.allowed_delegation_domains} "
                 f"agents={[l.agent.slug for l in app.allowed_agents.all()]}")
    return _out(app)


@connected_apps_router.post("/{slug}/connected-apps/{app_id}/rotate", response=SecretOut,
                            summary="Issue a new secret, invalidating the old one")
def rotate_secret(request: HttpRequest, slug: str, app_id: int) -> SecretOut:
    """The previous secret stops working immediately — that is the point of the
    button, since it is reached for when the old one has leaked."""
    app = _app_or_404(request, slug, app_id)
    secret = embed_apps.rotate(app)
    audit(event=EmbedAuditLog.ROTATE, request=request, app=app, actor=request.user)
    return SecretOut(secret=secret)


@connected_apps_router.delete("/{slug}/connected-apps/{app_id}", response={204: None},
                              summary="Disconnect a site")
def disconnect_app(request: HttpRequest, slug: str, app_id: int) -> Status:
    """Revoked rather than deleted: the row is the audit trail of what was once
    allowed to embed an agent, and its embed shell 404s from this moment."""
    app = _app_or_404(request, slug, app_id)
    embed_apps.revoke(app)
    audit(event=EmbedAuditLog.DISCONNECT, request=request, app=app, actor=request.user)
    return Status(204, None)
