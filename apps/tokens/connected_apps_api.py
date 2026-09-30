"""`/api/workspaces/{slug}/connected-apps` — connecting a site to canopy.

Mounted on the workspaces prefix rather than under `/api/tokens` because this
is a tenant-admin surface and shares its shape with the others there (members,
invites, the shared vault): `{slug}` in the path, owner-only, 404 before 403 so
a non-member cannot probe which workspaces exist.

The domain rules live in `embed_apps.py`, not here. This module is the HTTP
edge — statuses and serialisation.
"""

from __future__ import annotations

from typing import Literal

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
    "bad_host_url": 422,
    "already_shown": 409,
    "unknown_agent": 422,
}


class ConnectedAgentOut(Schema):
    slug: str
    name: str


class LiveProbeOut(Schema):
    """canopy's last live probe of this site: a real grant for the site's
    dedicated probe user, redeemed and used the way a visitor's turn uses one."""

    #: When it last ran; null if it never has.
    at: str | None
    #: true = every step passed; false = a step failed; null = no verdict (the
    #: site offers no probe, or it has not run yet).
    ok: bool | None
    #: The first step that failed, or why there was no verdict. Blank on a pass.
    step: str
    step_label: str
    #: Why, in words. Never contains a credential.
    reason: str


class TrafficHealthOut(Schema):
    """What real visitors' traffic says about this site."""

    #: The last time canopy redeemed a visitor's grant from this site.
    last_redeemed_at: str | None
    #: The last time an agent's call into this site, as a visitor, succeeded.
    last_site_call_at: str | None
    #: Refusals seen in the last 24 hours: refused redemptions (one per reason)
    #: plus refused calls.
    refusals_24h: int


class ConnectedAppOut(Schema):
    """A site connected to canopy.

    A site has no secret: it proves itself by signing (`jwks_url` or
    `public_keys`), so there is nothing here to protect or rotate.
    """

    id: int
    name: str
    origins: list[str]
    agents: list[ConnectedAgentOut]
    #: Registered PEM public keys. Returned in full — they are public by
    #: definition, and showing only a count would leave an operator unable to
    #: tell which key they are about to retire.
    public_keys: list[str]
    signs_assertions: bool
    #: Where the site publishes its keys, if it does. `signs_assertions` is
    #: true for either door — a published JWKS or a pasted key.
    jwks_url: str
    #: The site's OAuth issuer (RFC 8414) and MCP resource (RFC 9728), when it
    #: grants canopy access to its tools as the visitor (host grant contract
    #: v1). Both blank = it does not, and an agent cannot act for a visitor there.
    host_issuer: str
    host_mcp_resource: str
    #: Both are set, so arrivals carrying an `id_jag` are redeemed.
    issues_host_grants: bool
    # What the host last required of its visitors' runners — display only; the
    # signed claim is the authority.
    runner_requirements: list[str]
    shows_on_canopy_pages: bool
    created_at: str
    last_used_at: str | None
    revoked: bool
    live_probe: LiveProbeOut
    traffic: TrafficHealthOut


class ConnectIn(Schema):
    name: str
    origins: list[str] = []
    agents: list[str] = []
    public_keys: list[str] = []
    jwks_url: str = ""
    host_issuer: str = ""
    host_mcp_resource: str = ""
    show_on_canopy_pages: bool = False


class UpdateIn(Schema):
    origins: list[str] | None = None
    jwks_url: str | None = None
    agents: list[str] | None = None
    public_keys: list[str] | None = None
    host_issuer: str | None = None
    host_mcp_resource: str | None = None
    show_on_canopy_pages: bool | None = None


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _probe_out(app: AppCredential) -> LiveProbeOut:
    from . import live_probe

    return LiveProbeOut(at=_iso(app.last_probe_at), ok=app.last_probe_ok,
                        step=app.last_probe_step or "",
                        step_label=live_probe.STEPS.get(app.last_probe_step or "", app.last_probe_step or ""),
                        reason=app.last_probe_reason or "")


def _traffic_out(app: AppCredential) -> TrafficHealthOut:
    from . import live_probe

    health = live_probe.traffic_health(app)
    return TrafficHealthOut(last_redeemed_at=_iso(health.last_redeemed_at),
                            last_site_call_at=_iso(health.last_site_call_at),
                            refusals_24h=health.refusals_24h)


def _out(app: AppCredential) -> ConnectedAppOut:
    return ConnectedAppOut(
        id=app.pk,
        name=app.name,
        # The SANITISED list, matching what the browser will actually be sent.
        # Echoing the raw column would show a rejected origin as though it were
        # in force, which is the confusion `frame_origins()` exists to prevent.
        origins=app.frame_origins(),
        public_keys=list(app.public_keys or []),
        jwks_url=app.jwks_url or "",
        signs_assertions=bool(app.public_keys) or bool(app.jwks_url),
        host_issuer=app.host_issuer or "",
        host_mcp_resource=app.host_mcp_resource or "",
        issues_host_grants=app.issues_host_grants(),
        runner_requirements=list(app.last_runner_requirements or []),
        agents=[
            ConnectedAgentOut(slug=link.agent.slug, name=link.agent.name)
            for link in app.allowed_agents.all()
        ],
        shows_on_canopy_pages=app.show_on_canopy_pages,
        created_at=app.created_at.isoformat(),
        last_used_at=app.last_used_at.isoformat() if app.last_used_at else None,
        revoked=app.revoked_at is not None,
        live_probe=_probe_out(app),
        traffic=_traffic_out(app),
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
            host_issuer=payload.host_issuer, host_mcp_resource=payload.host_mcp_resource,
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
            jwks_url=payload.jwks_url, host_issuer=payload.host_issuer,
            host_mcp_resource=payload.host_mcp_resource,
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
          detail=f"origins={app.frame_origins()} "
                 f"agents={[l.agent.slug for l in app.allowed_agents.all()]}")
    return _out(app)


class ConnectionCheckOut(Schema):
    """One step of a connection test."""

    #: Stable id of the check (the SDK's name for it), e.g. `client_accepted`.
    name: str
    #: What it checks, in words.
    label: str
    status: Literal["pass", "fail", "skip"]
    #: Why it passed or failed. Never contains a credential.
    detail: str


class ConnectionTestOut(Schema):
    """What canopy found when it tried this site's settings, from its own server."""

    #: True when no check failed — the settings checks or the live probe
    #: (skipped checks do not count against it).
    ok: bool
    checks: list[ConnectionCheckOut]
    #: The live probe: a real grant for the site's dedicated probe user, issued,
    #: redeemed and used. Each step passes, fails or is skipped (a site with no
    #: probe identity skips it).
    live_probe: list[ConnectionCheckOut]
    #: true = the live probe passed; false = it failed; null = no verdict.
    live_probe_ok: bool | None


@connected_apps_router.post("/{slug}/connected-apps/{int:app_id}/test", response=ConnectionTestOut,
                            summary="Test a connected site's settings")
def test_connected_app(request: HttpRequest, slug: str, app_id: int) -> ConnectionTestOut:
    """Try this site's settings the way canopy uses them, from canopy's server.

    Reads the site's published keys, and — when it lets the agent act as the
    visitor — its sign-in and MCP discovery documents, then asks its token
    endpoint whether it accepts canopy as a client. That step sends a grant the
    site must refuse, so nothing is issued or used up there.

    Then the live probe, when the site offers one: canopy asks the site for a
    grant for its dedicated probe user, redeems it, calls the probe's tool with
    it, and checks that a tool outside its scope and a call without a valid
    proof are both refused. The result is recorded on the site, as it is when
    the probe runs on its schedule. Each step comes back as pass, fail or skip,
    with the reason.
    """
    # Rationale (not in the docstring — it is published): every URL here was
    # typed by a tenant, so every request goes through `outbound.py` (https,
    # no private address space, no redirects, bounded). The settings checks
    # cannot redeem a real grant (that needs an ID-JAG signed by the host's
    # key); the live probe can, because the HOST signs one for its own probe
    # principal. See apps/tokens/connection_test.py and live_probe.py.
    from . import connection_test, live_probe

    app = _app_or_404(request, slug, app_id)
    rows = connection_test.run(app)
    probe = live_probe.probe_and_record(app, trigger="test")
    if probe is None:
        probe_rows = [ConnectionCheckOut(name="probe_issued", label=live_probe.STEPS["probe_issued"],
                                         status="skip", detail="a probe of this site is already running")]
        probe_ok = None
    else:
        probe_rows = [ConnectionCheckOut(name=s.name, label=s.label, status=s.status, detail=s.detail)
                      for s in probe.steps]
        probe_ok = probe.ok
    return ConnectionTestOut(
        ok=not any(r.status == "fail" for r in rows) and probe_ok is not False,
        checks=[ConnectionCheckOut(name=r.name, label=r.label, status=r.status, detail=r.detail)
                for r in rows],
        live_probe=probe_rows,
        live_probe_ok=probe_ok,
    )


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
