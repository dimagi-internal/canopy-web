"""The site offer's two doors: `GET /api/sites/offer` (a connected site asks,
signed) and `GET /api/workspaces/{slug}/connected-apps/{id}/needs` (the
Connected sites page reads what the site asks back, and who meets it)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from canopy_sdk import offer as wire
from django.http import HttpRequest, HttpResponse
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.embedding import embed_apps
from apps.embedding.audit import client_ip
from apps.embedding.rate_limit import OfferRateLimitError, check_offer_client, check_offer_issuer

from . import client_identity, site_needs, site_offer

log = logging.getLogger(__name__)

sites_router = Router(tags=["connected apps"])
needs_router = Router(auth=session_auth, tags=["connected apps"])


def _bearer(request: HttpRequest) -> str:
    scheme, _, token = (request.headers.get("Authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HttpError(401, "no_assertion: send Authorization: Bearer <a request signed with your "
                             "site's key> (canopy_sdk.offer.request_assertion)")
    return token.strip()


@sites_router.get("/offer", auth=None, summary="A connected site's signed offer")
@gate("host")  # auth=None; a request signed by the site's own registered key IS the credential
def get_site_offer(request: HttpRequest):
    """Which agents this site may offer, and on what terms — signed by canopy.

    The site authenticates with `Authorization: Bearer <JWT>` signed by its own
    key (the one at its `jwks_url`): `iss` = its Connected-site name, `aud` =
    this URL, at most 60 s, single-use `jti`; `canopy_agent` names one of its
    agents when two workspaces registered the same name. The answer is
    `application/jose`: a compact JWS signed with canopy's client key (its `kid`
    in `/oauth/client.json`), payload `v`, `iss`, `aud`, `iat`, `exp`,
    `version`, `site`, `runner_flags`, `agents`. Verify it with
    `canopy_sdk.offer.fetch_offer`.
    """
    # Budgets in the order the work happens: per client before anything is
    # parsed, per site before its signature is checked.
    try:
        check_offer_client(client_ip(request))
    except OfferRateLimitError as exc:
        raise HttpError(429, f"rate_limited: {exc}")
    token = _bearer(request)
    try:
        claimed = site_offer.site_for_request(token)
        check_offer_issuer(f"{claimed.workspace_id}:{claimed.name}")
        app, _claims = site_offer.verify_request(token)
    except OfferRateLimitError as exc:
        raise HttpError(429, f"rate_limited: {exc}")
    except site_offer.OfferRefused as exc:
        raise HttpError(exc.status, f"{exc.code}: {exc.message}")
    try:
        body = site_offer.signed_offer(app)
    except client_identity.ClientIdentityError as exc:
        log.error("site offer for %s: %s", app.name, exc)
        raise HttpError(503, "not_configured: this canopy has no client key to sign with")
    response = HttpResponse(body, content_type=wire.MEDIA_TYPE)
    response["Cache-Control"] = "no-store"
    return response


class SitePageOut(Schema):
    key: str
    title: str
    scopes: list[str]


class RealDataRequiresOut(Schema):
    #: The site asks for a PHI-safe capability (`data: phi`).
    phi_safe: bool = False
    #: The runner flag the site asks for ("" for none).
    runner_flag: str = ""


class AgentFitOut(Schema):
    slug: str
    name: str
    #: The site says it offers this agent.
    offered: bool
    #: The site says it lets this agent read real data.
    real_data: bool
    #: canopy's offer says it can serve the site now.
    ready: bool
    #: It meets the site's `real_data.requires` and is ready.
    meets_real_data: bool
    #: Why not, each naming where in canopy to fix it.
    why_not: list[str]


class SiteNeedsOut(Schema):
    """What a connected site asks of canopy's agents (its signed `site.json`)."""

    #: Where canopy reads it: beside the site's `jwks.json`. "" = nowhere to read.
    url: str
    #: canopy holds a verified copy (possibly stale).
    available: bool
    #: Why it could not be read (or refreshed, when stale). Blank otherwise.
    reason: str
    fetched_at: str | None
    stale: bool
    pages: list[SitePageOut]
    requires: RealDataRequiresOut
    #: The offer version the site chose its settings against, and canopy's now.
    uses_version: str
    offer_version: str
    agents: list[AgentFitOut]
    #: Agents the site names that canopy does not allow on it — they never appear.
    not_allowed: list[str]


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@needs_router.get("/{slug}/connected-apps/{int:app_id}/needs", response=SiteNeedsOut,
                  summary="What a connected site asks of agents, and who meets it")
@gate("integrations")
def connected_app_needs(request: HttpRequest, slug: str, app_id: int) -> SiteNeedsOut:
    """The site's signed `site.json` — its pages and the scopes each grants, what
    an agent must be to read real data there, and which agents it offers — beside
    canopy's own offer for the site: per allowed agent, whether it meets the
    site's `real_data.requires` and, if not, why. Fetched like the site's keys
    (https only, bounded, cached) and fail-closed: an unreadable document is
    `available: false` with its reason."""
    try:
        app = embed_apps.apps_for(request.user, slug).filter(pk=app_id).first()
    except embed_apps.EmbedAppError as exc:
        raise HttpError(404 if exc.code == "not_found" else 403, f"{exc.code}: {exc.message}")
    if app is None:
        raise HttpError(404, "no such connected app in this workspace")
    needs = site_needs.needs_for(app)
    content = site_offer.offer_content(app)
    document = needs.document
    rows, not_allowed = site_needs.fit(document, content) if document else ([], [])
    requires = dict(document.real_data_requires) if document else {}
    return SiteNeedsOut(
        url=needs.url, available=document is not None, reason=needs.reason,
        fetched_at=_iso(needs.fetched_at), stale=needs.stale,
        pages=[SitePageOut(key=p.key, title=p.title, scopes=list(p.scopes))
               for p in (document.pages if document else ())],
        requires=RealDataRequiresOut(**requires),
        uses_version=document.uses_version if document else "",
        offer_version=wire.content_version(content),
        agents=[AgentFitOut(**r) for r in rows], not_allowed=not_allowed)
