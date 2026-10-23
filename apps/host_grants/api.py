"""The site agents API's doors: `GET /api/sites/agents` (a connected site asks,
signed), `POST /api/sites/resources-updated` (a connected site says its resources
changed, signed), `GET /api/workspaces/{slug}/connected-apps/{id}/needs` (the Connected
sites page reads what the site asks back, and who meets it) and
`GET /api/workspaces/{slug}/connected-apps/discover` (the page reads the site's
own facts from the same document, for its owner to adopt)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from canopy_sdk import site as site_doc
from canopy_sdk import site_agents as wire
from django.http import HttpRequest, HttpResponse
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.embedding import embed_apps
from apps.embedding.audit import client_ip
from apps.embedding.rate_limit import (
    SiteAgentsRateLimitError,
    check_resources_updated,
    check_site_agents_client,
    check_site_agents_issuer,
)
from apps.mcp_apps_host import mcp_apps_views
from apps.workspaces import permissions as perms

from . import client_identity, site_agents, site_needs
from . import resources_updated as notices

log = logging.getLogger(__name__)

sites_router = Router(tags=["connected apps"])
needs_router = Router(auth=session_auth, tags=["connected apps"])


def _bearer(request: HttpRequest) -> str:
    scheme, _, token = (request.headers.get("Authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HttpError(401, "no_assertion: send Authorization: Bearer <a request signed with your "
                             "site's key> (canopy_sdk.site_agents.request_assertion)")
    return token.strip()


@sites_router.get("/agents", auth=None, summary="The agents a connected site may show, signed")
@gate("host")  # auth=None; a request signed by the site's own registered key IS the credential
def get_site_agents(request: HttpRequest):
    """Which agents this site may show, and on what terms — signed by canopy.

    The site authenticates with `Authorization: Bearer <JWT>` signed by its own
    key (the one at its `jwks_url`): `iss` = its Connected-site name, `aud` =
    this URL, at most 60 s, single-use `jti`. `canopy_workspace` (the workspace
    slug) names which workspace's registration of the name is meant, or
    `canopy_agent` names one of its agents to the same end; both must agree
    (409 `workspace_mismatch`), and neither is needed unless two workspaces
    registered the same name (409 `ambiguous_issuer`). The answer is
    `application/jose`: a compact JWS signed with canopy's client key (its `kid`
    in `/oauth/client.json`), payload `v`, `iss`, `aud`, `iat`, `exp`,
    `version`, `site` (`name`, `tenant`, `settings_url` — this site's page in
    canopy), `runner_flags`, `agents` (each with `url`, `problems` and
    `problem_links`: `[{text, url}]`, the page that fixes each). Verify it with
    `canopy_sdk.site_agents.fetch_site_agents`.
    """
    # Budgets in the order the work happens: per client before anything is
    # parsed, per site before its signature is checked.
    try:
        check_site_agents_client(client_ip(request))
    except SiteAgentsRateLimitError as exc:
        raise HttpError(429, f"rate_limited: {exc}")
    token = _bearer(request)
    try:
        claimed = site_agents.site_for_request(token)
        check_site_agents_issuer(f"{claimed.workspace_id}:{claimed.name}")
        app, _claims = site_agents.verify_request(token)
    except SiteAgentsRateLimitError as exc:
        raise HttpError(429, f"rate_limited: {exc}")
    except site_agents.SiteAgentsRefused as exc:
        raise HttpError(exc.status, f"{exc.code}: {exc.message}")
    try:
        body = site_agents.signed_site_agents(app)
    except client_identity.ClientIdentityError as exc:
        log.error("site agents for %s: %s", app.name, exc)
        raise HttpError(503, "not_configured: this canopy has no client key to sign with")
    response = HttpResponse(body, content_type=wire.MEDIA_TYPE)
    response["Cache-Control"] = "no-store"
    return response


class ResourcesUpdatedOut(Schema):
    #: How many subscribed Views canopy told to re-read.
    notified: int


@sites_router.post("/resources-updated", auth=None, response=ResourcesUpdatedOut,
                   summary="A connected site says some of its resources changed, signed")
@gate("host", kind="action")  # auth=None; a request signed by the site's own registered key IS the credential
def resources_updated(request: HttpRequest):
    """Tell the Views subscribed to some of this site's resources to re-read them.

    The site authenticates with `Authorization: Bearer <JWT>` signed by its own key
    (the one at its `jwks_url`): `typ` `canopy-resources-updated+jwt`, `iss` = `sub`
    = its Connected-site name, `aud` = this URL, at most 60 s, single-use `jti`, and
    `uris` — the resource URIs that changed (1-50). `canopy_workspace` /
    `canopy_agent` name the tenant as for `GET /api/sites/agents`. Build and send it
    with `canopy_sdk.resources.notify_updated` (or the Django host's
    `artifacts.update` / `artifacts.notify_updated`). The body is ignored. Each View
    that subscribed (`resources/subscribe`) to one of the URIs on this site gets the
    socket frame `app.resource_updated` — the URI and its tool call id, never data —
    and re-reads it as its viewer. Answers `{notified}`.
    """
    try:
        check_resources_updated(client_ip(request))
    except SiteAgentsRateLimitError as exc:
        raise HttpError(429, f"rate_limited: {exc}")
    token = _bearer(request)
    try:
        claimed = site_agents.site_for_request(token)
        check_resources_updated(client_ip(request), f"{claimed.workspace_id}:{claimed.name}")
        app, claims = notices.verify(token)
    except SiteAgentsRateLimitError as exc:
        raise HttpError(429, f"rate_limited: {exc}")
    except site_agents.SiteAgentsRefused as exc:
        raise HttpError(exc.status, f"{exc.code}: {exc.message}")
    return {"notified": mcp_apps_views.resources_updated(app, claims["uris"])}


class SitePageOut(Schema):
    key: str
    title: str
    scopes: list[str]


class SensitiveDataRequiresOut(Schema):
    #: The site asks for a capability that handles sensitive data (`data: sensitive`).
    handles_sensitive: bool = False
    #: The runner flag the site asks for ("" for none).
    runner_flag: str = ""


class FitReasonOut(Schema):
    #: Why the agent cannot handle the site's sensitive data yet.
    text: str
    #: The page in canopy that fixes it ("" when there is none).
    url: str


class AgentFitOut(Schema):
    slug: str
    name: str
    #: The agent's page in canopy.
    url: str
    #: The site says it offers this agent.
    offered: bool
    #: The site says it lets this agent work with its sensitive data.
    sensitive_data: bool
    #: canopy's site agents API says it can serve the site now.
    ready: bool
    #: Only when `sensitive_data`: the agent meets the site's `sensitive_data.requires`
    #: and is ready. None when the site does not allow it sensitive data — there is
    #: nothing to meet.
    can_handle_sensitive_data: bool | None
    #: Why it cannot yet, each with where in canopy to fix it. Empty unless
    #: `can_handle_sensitive_data` is false.
    why_not: list[FitReasonOut]


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
    requires: SensitiveDataRequiresOut
    #: The site agents version the site chose its settings against, and canopy's now.
    uses_version: str
    site_agents_version: str
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
    """The site's signed `site.json` — which agents it offers, which of them it
    lets work with its sensitive data and what such an agent must be, and its
    pages with the scopes each grants (the site's own description; canopy never
    enforces it) — beside canopy's own site agents response for the site: per
    allowed agent the site lets near its sensitive data, whether it can handle it
    now and, if not, why. Fetched like the site's keys
    (https only, bounded, cached) and fail-closed: an unreadable document is
    `available: false` with its reason."""
    try:
        app = embed_apps.apps_for(request.user, slug).filter(pk=app_id).first()
    except embed_apps.EmbedAppError as exc:
        raise HttpError(404 if exc.code == "not_found" else 403, f"{exc.code}: {exc.message}")
    if app is None:
        raise HttpError(404, "no such connected app in this workspace")
    needs = site_needs.needs_for(app)
    content = site_agents.site_agents_content(app)
    document = needs.document
    rows, not_allowed = site_needs.fit(document, content) if document else ([], [])
    requires = dict(document.sensitive_data_requires) if document else {}
    return SiteNeedsOut(
        url=needs.url, available=document is not None, reason=needs.reason,
        fetched_at=_iso(needs.fetched_at), stale=needs.stale,
        pages=[SitePageOut(key=p.key, title=p.title, scopes=list(p.scopes))
               for p in (document.pages if document else ())],
        requires=SensitiveDataRequiresOut(**requires),
        uses_version=document.uses_version if document else "",
        site_agents_version=wire.content_version(content),
        agents=[AgentFitOut(**r) for r in rows], not_allowed=not_allowed)


class SiteFactsOut(Schema):
    #: The origins (scheme, host, port) whose pages may frame the agent panel.
    origins: list[str]
    #: Where the site publishes its signing keys.
    jwks_url: str
    #: The site's OAuth issuer and MCP server, when it grants canopy access to its
    #: tools as the visitor ("" when it does not).
    issuer: str
    mcp_resource: str


class SiteDiscoveryOut(Schema):
    """What a site's own signed `site.json` says about it — for an owner to adopt,
    never applied by this read."""

    #: Where the document was read, and every place tried to find it.
    url: str
    tried: list[str]
    #: The site's Connected-site name, as the site signs it.
    name: str
    #: What the site declares.
    site: SiteFactsOut
    #: `registered_keys`: verified with the keys canopy already holds for this
    #: site. `published_keys`: verified with the keys at the `jwks_url` the
    #: document itself declares — adopting it then also adopts those keys.
    signed_by: str
    #: Whether canopy's registered keys verify it; null when none are registered.
    registered_keys_verify: bool | None
    #: For a registered site: what canopy holds now, and which fields differ.
    current: SiteFactsOut | None = None
    changes: list[str] = []


def _facts_out(origins, jwks_url, issuer, mcp_resource) -> SiteFactsOut:
    return SiteFactsOut(origins=list(origins), jwks_url=jwks_url or "", issuer=issuer or "",
                        mcp_resource=mcp_resource or "")


@needs_router.get("/{slug}/connected-apps/discover", response=SiteDiscoveryOut,
                  summary="Read a site's own facts from its signed site.json")
@gate("own")
def discover_site(request: HttpRequest, slug: str, address: str = "",
                  app_id: int | None = None) -> SiteDiscoveryOut:
    """Reads the site's signed `site.json` and returns the facts it declares
    about itself — its origins, the URL of its keys, its sign-in issuer and its
    MCP server — so an owner can fill a Connected site from them instead of
    typing them. `address` is the site's address, the URL of its keys, or of the
    document itself; the document is looked for beside its keys, at
    `/.well-known/canopy-site.json` and at `canopy/site.json`. With `app_id` (a
    site this workspace registered) `address` is optional, the document must be
    that site's, and the answer includes what canopy holds now and which fields
    differ. Nothing is saved: adopt the values with the create or update route."""
    # Rationale (not in the docstring — it is published): owner-gated because it
    # is the read half of an owner-only write and makes outbound requests to a
    # URL the caller typed (https only, public addresses, no redirects —
    # canopy_sdk.fetch and apps/embedding/jwks.py). It never writes: a site
    # changing its own trusted origins needs the owner to save them.
    try:
        embed_apps.require(request.user, slug, perms.OWN)
        app = None
        if app_id is not None:
            app = embed_apps.apps_for(request.user, slug).filter(pk=app_id).first()
            if app is None:
                raise HttpError(404, "no such connected app in this workspace")
    except embed_apps.EmbedAppError as exc:
        raise HttpError(404 if exc.code == "not_found" else 403, f"{exc.code}: {exc.message}")
    try:
        found = site_needs.discover(address, app=app)
    except site_doc.SiteDocumentError as exc:
        raise HttpError(422, f"{exc.code}: {exc.message}")
    facts = found.facts
    out = SiteDiscoveryOut(
        url=found.url, tried=list(found.tried), name=found.name,
        site=_facts_out(facts.origins, facts.jwks_url, facts.issuer, facts.mcp_resource),
        signed_by=found.signed_by, registered_keys_verify=found.registered_keys_verify)
    if app is not None:
        current = _facts_out(app.frame_origins(), app.jwks_url, app.host_issuer, app.host_mcp_resource)
        out.current = current
        out.changes = [f for f in ("origins", "jwks_url", "issuer", "mcp_resource")
                       if getattr(current, f) != getattr(out.site, f)]
    return out
