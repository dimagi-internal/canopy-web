"""A person's own host grants: confirm them, see them, and stop them.

A host grant (`tokens.HostGrant`, `host_grants.py`) lets an agent read a connected
site's tools AS the person, for the page they sent from — once they confirmed it
with their passkey. Only a canopy user holds one; a contact never does. The person
sees and stops them in two places, reading the same rows (`host_grants.live_for`,
`host_grants.revoke`):

* **Settings** — `GET /api/me/host-grants/` and `DELETE /api/me/host-grants/{id}`:
  every live grant redeemed for your account, whichever site or workspace it was
  minted on. It is your own credential, like a token on the same page, so it is
  read by person, not per organisation: a site in a workspace you are not a member
  of can still hold a grant for you.
* **The panel** — `GET /api/embed/host-grant` and `DELETE /api/embed/host-grant`:
  the grant the panel's own token carries (the page it is on), for the "can act as
  you on this page · Stop" line, or the passkey prompt while it waits.

**Confirming** is the panel's too: `POST /api/embed/host-grant/confirm/begin` and
`/finish` are a WebAuthn assertion with one of the token's user's own passkeys,
made in canopy's frame. The panel's token is held by the site that framed it, so
the token alone proves nothing about the person; the assertion does — it is signed
over canopy's origin, which no other page can produce. A confirmed grant lasts
`host_grants.CONFIRM_SECONDS` from the touch. A panel whose frame cannot use a
passkey (the Canopy Runner app) has the person confirm the same grant in their
browser instead: `/api/me/host-grants/{id}/confirm/begin|finish`, on their own
browser session (the section at the end).

Stopping a grant stops the page's whole permission (`host_grants.same_page`: the
panel re-mints while its tab is open, so one page's permission is several rows),
at once: the gateway and Views refuse it from the next call.
"""
from __future__ import annotations

from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema, Status

from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError
from apps.api.route_gates import gate
from apps.events import audit
from apps.passkeys import services as passkeys
from apps.passkeys import stepup
from apps.passkeys.schemas import PasskeyAssertIn, PasskeyOptionsOut
from apps.tokens.models import HostGrant

from . import host_grants

me_router = Router(auth=session_auth, tags=["host grants"])
embed_grant_router = Router(auth=session_auth, tags=["host grants"])


class HostGrantOut(Schema):
    id: int
    #: The connected site's name — whose tools the agent reads as you.
    site: str
    #: The agent the page chose ("" = any agent the site offers).
    agent: str = ""
    agent_name: str = ""
    #: What the page granted, as the site names it (space-separated scopes).
    scope: str = ""
    #: Since when this page's permission has been held (its newest grant).
    granted_at: datetime
    #: When it runs out: 30 minutes after the passkey confirmed it, at most.
    expires_at: datetime
    #: Whether the person confirmed it with their passkey. Until then no agent may use it.
    confirmed: bool = False
    #: Whether an agent's turn needs it while it still waits for the passkey: the
    #: panel asks the person only then, never merely because they sent.
    requested: bool = False
    #: What the agent said it needs it for, in its own words ("" = it gave none).
    requested_reason: str = ""


class PanelGrantOut(Schema):
    #: The grant the panel's own token carries — confirmed, or waiting for the
    #: person's passkey — or null: none, run out or stopped.
    grant: HostGrantOut | None = None


def _out(grant) -> HostGrantOut:
    return HostGrantOut(
        id=grant.pk, site=grant.app.name,
        agent=grant.agent.slug if grant.agent_id else "",
        agent_name=(grant.agent.name or grant.agent.slug) if grant.agent_id else "",
        scope=grant.scope, granted_at=grant.created_at, expires_at=grant.expires_at,
        confirmed=grant.confirmed_at is not None,
        requested=grant.requested_at is not None,
        requested_reason=grant.requested_reason or "")


def _stop(grant) -> int:
    """Revoke `grant`'s page, reported to the settings audit."""
    stopped = host_grants.revoke(grant)
    audit.note_change(scope="host_grant",
                      before={"site": grant.app.name, "agent": grant.agent.slug if grant.agent_id else "",
                              "scope": grant.scope, "live": stopped},
                      after={"site": grant.app.name, "live": 0},
                      summary=f"stopped {grant.app.name}'s permission to read as them ({grant.scope or '-'})")
    return stopped


# --- Settings ---------------------------------------------------------------------


@me_router.get("/me/host-grants/", response=list[HostGrantOut],
               summary="The sites that may let an agent read as me, right now")
@gate("self")
def list_my_host_grants(request: HttpRequest) -> list[HostGrantOut]:
    """Every live host grant held for you: which site, which agent, what it may
    read, and until when — one row per page you sent from. A grant is taken only
    once you send a message in a site's panel and confirm it with your passkey,
    and lasts 30 minutes from that confirmation."""
    return [_out(g) for g in host_grants.live_for(user=request.user)]


@me_router.delete("/me/host-grants/{int:grant_id}", response={204: None},
                  summary="Stop a site letting an agent read as me")
@gate("self", setting=True)
def revoke_my_host_grant(request: HttpRequest, grant_id: int) -> Status:
    """Stop this grant, and every live grant of the same page, at once: an agent
    can no longer read that site as you until you send from its page again."""
    grant = HostGrant.objects.filter(pk=grant_id, user=request.user).select_related("app", "agent").first()
    if grant is None:
        raise ProblemError(404, "No such grant", type_=TYPE_NOT_FOUND)
    _stop(grant)
    return Status(204, None)


# --- the panel ------------------------------------------------------------------------


def _token_grant(request):
    """The unexpired grant the request's own token carries — the page the panel is
    on — confirmed or still waiting for the person's passkey. A user's only."""
    grant_id = getattr(request, "host_grant_id", None)
    if not grant_id or getattr(request, "contact", None) is not None:
        return None
    rows = HostGrant.objects.filter(pk=grant_id, user=request.user, contact__isnull=True)
    return host_grants.unexpired(rows).select_related("app", "agent").first()


@embed_grant_router.get("/host-grant", response=PanelGrantOut,
                        summary="What this panel's page lets the agent do as me")
@gate("self")
def panel_host_grant(request: HttpRequest) -> PanelGrantOut:
    """The grant the panel's own token carries — the page it is on — or null. The
    panel shows a confirmed one as "can act as you on this page until …", and asks
    for your passkey while one waits (`confirmed: false`)."""
    grant = _token_grant(request)
    return PanelGrantOut(grant=_out(grant) if grant is not None else None)


@embed_grant_router.delete("/host-grant", response=PanelGrantOut,
                           summary="Stop this panel's page letting the agent act as me")
@gate("self", setting=True)
def stop_panel_host_grant(request: HttpRequest) -> PanelGrantOut:
    """Stop the grant the panel's token carries, and every live grant of the same
    page. The panel then asks for none until you send again."""
    grant = _token_grant(request)
    if grant is not None:
        _stop(grant)
    return PanelGrantOut(grant=None)


def _waiting_grant(request):
    grant = _token_grant(request)
    if grant is None:
        raise ProblemError(404, "No permission is waiting on this page", type_=TYPE_NOT_FOUND,
                           detail="Send from the panel to ask for one.")
    return grant


def _slot(grant) -> str:
    # One ceremony per grant: a challenge begun for one page cannot confirm another.
    return f"host-grant:{grant.pk}"


# The panel's own token, deliberately — not a browser session: on a site's page the
# frame has none. What makes it the person is the passkey assertion over canopy's
# origin, which the site holding the token cannot produce (module docstring).
@embed_grant_router.post("/host-grant/confirm/begin", response=PasskeyOptionsOut,
                         summary="Start confirming this page's act-as-you permission with my passkey")
@gate("self", kind="ephemeral-credential")  # a ceremony's challenge, for the person's own grant
def begin_confirm_host_grant(request: HttpRequest) -> PasskeyOptionsOut:
    """Options for navigator.credentials.get(), to confirm the permission this
    panel's page is waiting on with one of your passkeys."""
    stepup.require_a_person(request, "confirming an act-as-you permission")
    return _begin_confirm(request, _waiting_grant(request))


@embed_grant_router.post("/host-grant/confirm/finish", response=PanelGrantOut,
                         summary="Confirm this page's act-as-you permission with my passkey")
@gate("self", setting=True)
def finish_confirm_host_grant(request: HttpRequest, payload: PasskeyAssertIn) -> PanelGrantOut:
    """Verify the passkey assertion and confirm the permission: from now, for 30
    minutes, the agent may act as you on this page."""
    stepup.require_a_person(request, "confirming an act-as-you permission")
    grant = _waiting_grant(request)
    _confirm(request, grant, payload.credential)
    return PanelGrantOut(grant=_out(grant))


def _begin_confirm(request: HttpRequest, grant) -> PasskeyOptionsOut:
    options = passkeys.begin_assertion(request, slot=_slot(grant))
    if options is None:
        raise ProblemError(
            409, "No passkey registered", type_=TYPE_CONFLICT,
            detail="Register a passkey (canopy Settings → Passkeys), then send again.",
            members={"error": "no_passkey", "register_url": stepup.REGISTER_PATH},
        )
    return PasskeyOptionsOut(options=options)


def _confirm(request: HttpRequest, grant, credential: dict) -> None:
    """Verify the assertion begun for `grant` and confirm it — the one confirm both
    the panel and the browser page run."""
    try:
        passkeys.finish_assertion(request, credential, slot=_slot(grant))
    except passkeys.CeremonyError as exc:
        raise ProblemError(400, "Passkey verification failed", type_=TYPE_VALIDATION,
                           detail=str(exc)) from exc
    if grant.confirmed_at is None:
        host_grants.confirm(grant)
        audit.note_change(scope="host_grant",
                          before={"site": grant.app.name, "scope": grant.scope, "confirmed": False},
                          after={"site": grant.app.name, "scope": grant.scope, "confirmed": True,
                                 "until": grant.expires_at.isoformat()},
                          summary=f"confirmed with a passkey: {grant.app.name} may be read as them "
                                  f"({grant.scope or '-'})")
        # MCP Apps: list the site's tools as the person now that they may be, so a
        # result from the agent's own login (path D) finds its View.
        from apps.mcp_apps_host import mcp_apps_views  # noqa: PLC0415 — as contact_api's: loads at the confirm only

        mcp_apps_views.refresh_index_soon(grant.app, grant)


# --- in the person's browser ------------------------------------------------------------
# The panel's own confirm needs WebAuthn in its frame, and some frames have none: the
# Canopy Runner desktop app hosts canopy in a web view with no passkey access. There the
# panel opens `/confirm/page-permission/<id>` in the person's browser, which confirms
# the same waiting grant through these routes with THEIR signed-in browser session and
# their passkey, and the panel sees it confirmed on its next read. The grant id alone
# confirms nothing: the session must be the grant's own person, at a browser (a
# token-minted session is refused), and the assertion is theirs over canopy's origin.


def _my_grant(request: HttpRequest, grant_id: int):
    rows = HostGrant.objects.filter(pk=grant_id, user=request.user, contact__isnull=True)
    grant = host_grants.unexpired(rows).select_related("app", "agent").first()
    if grant is None:
        raise ProblemError(404, "No such permission", type_=TYPE_NOT_FOUND,
                           detail="It was stopped, or it ran out. Send from the page again to ask for one.")
    return grant


def _my_waiting_grant(request: HttpRequest, grant_id: int):
    stepup.require_browser(request, "confirming an act-as-you permission")
    stepup.require_a_person(request, "confirming an act-as-you permission")
    return _my_grant(request, grant_id)


@me_router.get("/me/host-grants/{int:grant_id}", response=HostGrantOut,
               summary="One page's act-as-you permission, confirmed or waiting for my passkey")
@gate("self")
def get_my_host_grant(request: HttpRequest, grant_id: int) -> HostGrantOut:
    """One of your unexpired act-as-you permissions — which site, which agent, what it
    may do — confirmed, or still waiting for your passkey (`confirmed: false`)."""
    return _out(_my_grant(request, grant_id))


@me_router.post("/me/host-grants/{int:grant_id}/confirm/begin", response=PasskeyOptionsOut,
                summary="Start confirming a page's act-as-you permission with my passkey, in my browser")
@gate("self", kind="ephemeral-credential")  # a ceremony's challenge, for the person's own grant
def begin_confirm_my_host_grant(request: HttpRequest, grant_id: int) -> PasskeyOptionsOut:
    """Options for navigator.credentials.get(), to confirm a permission a page is
    waiting on — the same confirmation as the page's own panel, made in your browser
    when the panel cannot use a passkey."""
    return _begin_confirm(request, _my_waiting_grant(request, grant_id))


@me_router.post("/me/host-grants/{int:grant_id}/confirm/finish", response=HostGrantOut,
                summary="Confirm a page's act-as-you permission with my passkey, in my browser")
@gate("self", setting=True)
def finish_confirm_my_host_grant(request: HttpRequest, grant_id: int,
                                 payload: PasskeyAssertIn) -> HostGrantOut:
    """Verify the passkey assertion and confirm the permission: from now, for 30
    minutes, the agent may act as you on that page. The page's panel sees it at once."""
    grant = _my_waiting_grant(request, grant_id)
    _confirm(request, grant, payload.credential)
    return _out(grant)
