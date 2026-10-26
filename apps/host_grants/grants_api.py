"""A person's own host grants: see them, and stop them.

A host grant (`tokens.HostGrant`, `host_grants.py`) lets an agent read a connected
site's tools AS the person, for the page they sent from. The person sees and stops
them in three places, all reading the same rows (`host_grants.live_for`,
`host_grants.revoke`):

* **Settings** — `GET /api/me/host-grants/` and `DELETE /api/me/host-grants/{id}`:
  every live grant redeemed for your account, whichever site or workspace it was
  minted on. It is your own credential, like a token on the same page, so it is
  read by person, not per organisation: a site in a workspace you are not a member
  of can still hold a grant for you.
* **The panel, as a canopy user** — `GET /api/embed/host-grant` and
  `DELETE /api/embed/host-grant`: the grant the panel's own token carries (the
  page it is on), for the "can read what this page shows, as you · Stop" line.
* **The panel, as a contact** — `GET /api/contact/host-grant` and
  `DELETE /api/contact/host-grant`: the same, on the contact surface.

Stopping a grant stops the page's whole permission (`host_grants.same_page`: the
panel re-mints while its tab is open, so one page's permission is several rows),
at once: the gateway and Views refuse it from the next call.
"""
from __future__ import annotations

from datetime import datetime

from django.http import HttpRequest
from ninja import Router, Schema, Status

from apps.api.auth import session_auth
from apps.api.errors import TYPE_NOT_FOUND, ProblemError
from apps.api.route_gates import gate
from apps.contacts.contact_api import contact_auth
from apps.events import audit
from apps.tokens.models import HostGrant

from . import host_grants

me_router = Router(auth=session_auth, tags=["host grants"])
embed_grant_router = Router(auth=session_auth, tags=["host grants"])
#: The contact surface's twin (`/api/contact/host-grant`), on the contact's own auth.
contact_grant_router = Router(auth=contact_auth, tags=["contact"])


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
    #: When it runs out, unless the page re-grants first.
    expires_at: datetime


class PanelGrantOut(Schema):
    #: The live grant the panel's own token carries, or null: none, run out or stopped.
    grant: HostGrantOut | None = None


def _out(grant) -> HostGrantOut:
    return HostGrantOut(
        id=grant.pk, site=grant.app.name,
        agent=grant.agent.slug if grant.agent_id else "",
        agent_name=(grant.agent.name or grant.agent.slug) if grant.agent_id else "",
        scope=grant.scope, granted_at=grant.created_at, expires_at=grant.expires_at)


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
    once you send a message in a site's panel, and lasts 30 minutes unless that
    page grants again."""
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
    """The live grant the request's own token carries — the page the panel is on."""
    grant_id = getattr(request, "host_grant_id", None)
    if not grant_id:
        return None
    rows = HostGrant.objects.filter(pk=grant_id).select_related("app", "agent")
    contact = getattr(request, "contact", None)
    rows = rows.filter(contact=contact) if contact is not None else rows.filter(user=request.user)
    return host_grants.live(rows).first()


@embed_grant_router.get("/host-grant", response=PanelGrantOut,
                        summary="What this panel's page lets the agent read as me")
@gate("self")
def panel_host_grant(request: HttpRequest) -> PanelGrantOut:
    """The live grant the panel's own token carries — the page it is on — or
    null. The panel shows it as "can read what this page shows, as you, until …"."""
    grant = _token_grant(request)
    return PanelGrantOut(grant=_out(grant) if grant is not None else None)


@embed_grant_router.delete("/host-grant", response=PanelGrantOut,
                           summary="Stop this panel's page letting the agent read as me")
@gate("self", setting=True)
def stop_panel_host_grant(request: HttpRequest) -> PanelGrantOut:
    """Stop the grant the panel's token carries, and every live grant of the same
    page. The panel then asks for none until you send again."""
    grant = _token_grant(request)
    if grant is not None:
        _stop(grant)
    return PanelGrantOut(grant=None)


@contact_grant_router.get("/host-grant", response=PanelGrantOut,
                          summary="What this panel's page lets the agent read as me")
@gate("contact")
def contact_host_grant(request: HttpRequest) -> PanelGrantOut:
    """The live grant your panel's token carries — the page it is on — or null."""
    grant = _token_grant(request)
    return PanelGrantOut(grant=_out(grant) if grant is not None else None)


@contact_grant_router.delete("/host-grant", response=PanelGrantOut,
                             summary="Stop this panel's page letting the agent read as me")
@gate("contact", setting=True)
def contact_stop_host_grant(request: HttpRequest) -> PanelGrantOut:
    """Stop the grant your panel's token carries, and every live grant of the
    same page. The panel asks for none until you send again."""
    grant = _token_grant(request)
    if grant is not None:
        _stop(grant)
    return PanelGrantOut(grant=None)
