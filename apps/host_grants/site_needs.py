"""What a connected site asks of canopy's agents (its signed `site.json`), and
whether each agent canopy allows there meets it (spec connect-labs
`2026-10-22-canopy-site-agents-api-design` §2).

The site publishes `site.json` beside its `jwks.json` (the SDK's host route), so
canopy needs no new field to find it: `canopy_sdk.site.site_document_url`. It
is fetched the way the site's keys are — https only, address-pinned, no
redirects, bounded (`canopy_sdk.fetch`) — cached, and FAIL-CLOSED: no
verifiable document is shown as unavailable with its reason, never as "meets".
A copy is reused for `FRESH_SECONDS`; when a refetch fails the last good copy
is shown marked stale for up to `STALE_SECONDS`.

It is verified with the site's registered keys (`assertions.keys_for_app`, the
same that verify its visitor assertions), and `iss` must be the site's name.

`fit` compares it with canopy's own site agents response for the site (`site_agents.
site_agents_content`), so the Connected sites page and the response the site reads can
never disagree about an agent.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import jwt
from canopy_sdk import fetch as sdk_fetch
from canopy_sdk import site as wire
from canopy_sdk.jose import unverified_claims
from django.core.cache import cache

from apps.embedding import assertions
from apps.embedding import jwks as jwks_mod
from apps.mcp_apps_host.mcp_apps_views import site_app

FRESH_SECONDS = 600
STALE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class Needs:
    """The site's document as canopy last read it — or why it could not."""

    url: str
    document: wire.SiteNeeds | None
    fetched_at: float | None = None
    stale: bool = False
    reason: str = ""


def _get(url: str) -> str:
    return sdk_fetch.get_text(url, accept=wire.MEDIA_TYPE)


def _fetch(app, url: str) -> wire.SiteNeeds:
    try:
        token = _get(url)
    except sdk_fetch.FetchError as exc:
        raise wire.SiteDocumentError("unreachable", str(exc)) from exc
    try:
        keys = assertions.keys_for_app(app, token)
    except assertions.AssertionError_ as exc:
        raise wire.SiteDocumentError(exc.code, exc.message) from exc
    return wire.verify_site_document(token, keys, site_name=app.name,
                                     algorithms=assertions.ALLOWED_ALGORITHMS)


def _keys(app, url: str) -> tuple[str, str]:
    # The signer is in the key: a site that changes its keys is re-read at once,
    # never vouched for by a copy its old key signed.
    scope = f"{app.pk}:{app.signer()}:{url}"
    return f"site-needs:fresh:{scope}", f"site-needs:last-good:{scope}"


def document_url(jwks_url: str) -> str:
    """Where a site's `site.json` is: beside its keys. The SDK's rule
    (`…/jwks.json` → `…/site.json`), plus a site that serves its keys at its own
    `…/jwks/` route (connect-labs: `/labs/canopy/jwks/`) → `…/site.json` beside
    that route. "" when the URL names neither."""
    url = wire.site_document_url(jwks_url)
    if url:
        return url
    head, _, name = str(jwks_url or "").strip().rstrip("/").rpartition("/")
    return f"{head}/{wire.SITE_DOCUMENT_NAME}" if head and name == "jwks" else ""


def needs_for(app, *, now: float | None = None) -> Needs:
    t = time.time() if now is None else now
    url = document_url(app.jwks_url)
    if not url:
        return Needs(url="", document=None, reason=(
            "this site publishes no keys at a …/jwks.json (or …/jwks/) URL, so canopy has "
            "nowhere to read its site.json — serve the SDK's canopy/site.json beside its keys"))
    fresh, last_good = _keys(app, url)
    entry = cache.get(fresh)
    if isinstance(entry, dict):
        return Needs(url, wire.SiteNeeds.from_claims(entry["claims"], site_name=app.name),
                     entry["fetched_at"])
    try:
        document = _fetch(app, url)
    except wire.SiteDocumentError as exc:
        reason = f"{exc.code}: {exc.message}"
        entry = cache.get(last_good)
        if isinstance(entry, dict) and t - entry["fetched_at"] <= STALE_SECONDS:
            return Needs(url, wire.SiteNeeds.from_claims(entry["claims"], site_name=app.name),
                         entry["fetched_at"], stale=True, reason=reason)
        return Needs(url, None, reason=reason)
    entry = {"claims": document.claims, "fetched_at": t}
    cache.set(fresh, entry, FRESH_SECONDS)
    cache.set(last_good, entry, STALE_SECONDS + FRESH_SECONDS)
    return Needs(url, document, t)


def _short(text: str) -> str:
    """A site agents problem sentence without its trailing `canopy › <url>` (the
    url travels beside it)."""
    return text.split(": canopy › ")[0].strip()


def fit(document: wire.SiteNeeds, content: dict) -> tuple[list[dict], list[str]]:
    """Per allowed agent: does the site offer it, does it let it work with its
    sensitive data, and — only then — can the agent handle that data now, and if
    not, why (each reason with the page in canopy that fixes it). An agent the
    site does not allow near its sensitive data has nothing to meet:
    `can_handle_sensitive_data` is None and `why_not` empty. Plus the agents the
    site names that canopy does not allow there (they can never appear).

    The site's pages and scopes are not part of the fit: they are the site's own
    description, enforced by the site, and canopy never tells an agent about them."""
    requires = dict(document.sensitive_data_requires or {})
    flag = requires.get("runner_flag") or ""
    # The site agents API's sentence for the missing runner, said once below.
    runner_problem = f"no ready runner with {flag}" if flag else None
    rows = []
    for agent in content["agents"]:
        allowed = agent["slug"] in document.uses_sensitive_data_agents
        settings_url = f"{agent['url']}/settings"
        why: list[dict] = []
        if allowed:
            links = agent.get("problem_links") or [{"text": p, "url": ""} for p in agent["problems"]]
            if requires.get("handles_sensitive") and not any(
                    c["handles_sensitive"] for c in agent["capabilities"]):
                why.append({"text": f"no capability of {agent['name']} for this site says "
                                    f"`data: sensitive`", "url": f"{settings_url}#reach"})
            if flag and not agent["runners"]["ready_with"].get(flag):
                # The site agents API's own link when it has one: it names where to fix it.
                found = next((p for p in links if p["text"].startswith(runner_problem)), None)
                why.append({"text": f"no ready {flag.replace('_', '-')} runner takes its turns",
                            "url": (found or {}).get("url") or f"{settings_url}#running"})
            why += [{"text": _short(p["text"]), "url": p["url"]} for p in links
                    if not (runner_problem and p["text"].startswith(runner_problem))]
        rows.append({
            "slug": agent["slug"],
            "name": agent["name"],
            "url": agent["url"],
            "offered": agent["slug"] in document.uses_agents,
            "sensitive_data": allowed,
            "ready": agent["ready"],
            "can_handle_sensitive_data": (agent["ready"] and not why) if allowed else None,
            "why_not": why,
        })
    allowed_slugs = {a["slug"] for a in content["agents"]}
    named = list(dict.fromkeys([*document.uses_agents, *document.uses_sensitive_data_agents]))
    return rows, [s for s in named if s not in allowed_slugs]


# --- the site's own facts: filling a Connected site from its document ----------------
#
# A Connected site's registration is a copy of facts the site already knows —
# its origins, where its keys are, its OAuth issuer, its MCP server — and its
# `site.json` (SDK 0.16+) declares them in a `site` block. `discover` reads that
# block so the owner can ADOPT the values instead of typing them. It only reads:
# the values reach the registration solely through the ordinary, audited
# update/create routes, after the owner has seen them — a site must never be
# able to change its own trusted origins by publishing a document.


@dataclass(frozen=True)
class Discovery:
    """What a site's own document says about it, and how canopy believes it."""

    #: Where the document was read.
    url: str
    #: The site's Connected-site name (`iss`).
    name: str
    facts: wire.SiteFacts
    #: `registered_keys` — verified with the keys canopy already holds for the
    #: site; `published_keys` — with the keys at the `jwks_url` the document
    #: itself declares (a site not yet registered, or one whose keys moved).
    signed_by: str
    #: Whether canopy's registered keys verify it (None: none registered).
    registered_keys_verify: bool | None
    #: Every URL tried, in order (the last one is `url`).
    tried: tuple[str, ...] = ()


def candidate_urls(address: str) -> list[str]:
    """Where a site's `site.json` may be, given whatever address an owner has:
    the document itself, its JWKS (`document_url`), or just the site's address —
    then the address's own `…/site.json` (an SDK mount typed in), the origin's
    `.well-known/canopy-site.json` (`wire.WELL_KNOWN_PATH`), and the SDK's default
    mount `…/canopy/site.json`."""
    url = (address or "").strip()
    if not url:
        return []
    if "://" not in url:
        url = f"https://{url}"
    parts = urlsplit(url)
    if not parts.netloc:
        return []
    path = parts.path.rstrip("/")
    if path.endswith(f"/{wire.SITE_DOCUMENT_NAME}"):
        return [url]
    beside = document_url(url)
    if beside:
        return [beside]
    origin = f"{parts.scheme}://{parts.netloc}"
    out = [f"{origin}{path}/{wire.SITE_DOCUMENT_NAME}"] if path else []
    out += [f"{origin}{wire.WELL_KNOWN_PATH}", f"{origin}{path}/canopy/{wire.SITE_DOCUMENT_NAME}"]
    return list(dict.fromkeys(out))


def _published_keys(jwks_url: str, token: str) -> list:
    try:
        kid = str((jwt.get_unverified_header(token) or {}).get("kid") or "")
    except Exception:  # noqa: BLE001 - the verifier refuses it with its own reason
        kid = ""
    if not jwks_url.startswith("https://"):
        raise wire.SiteDocumentError("bad_jwks_url", f"the keys the document names ({jwks_url}) "
                                                     "are not at an https URL")
    try:
        # `keys_for` refuses private address space itself, as for any site's keys.
        return jwks_mod.keys_for(jwks_url, kid=kid)
    except jwks_mod.JwksError as exc:
        raise wire.SiteDocumentError("keys_unreachable", f"could not read the keys the document "
                                                         f"names ({jwks_url}): {exc}") from exc


def _verifies(token: str, keys: list, name: str) -> bool:
    try:
        wire.verify_site_document(token, keys, site_name=name, algorithms=assertions.ALLOWED_ALGORITHMS)
    except wire.SiteDocumentError as exc:
        if exc.code == "bad_signature":
            return False
        raise
    return True


def discover(address: str = "", *, app=None) -> Discovery:
    """Read and verify the site's own document and return its `site` facts.
    Raises `wire.SiteDocumentError` (`no_address`, `unreachable`, `bad_issuer`,
    `bad_signature`, `no_facts`, …) with a reason the owner can act on.

    `address` may be the site's address, its JWKS URL or the document's own; for
    a registered `app` it defaults to beside its keys, then its first origin.
    Fresh every time (no cache): the owner is about to act on what it says.
    """
    if not (address or "").strip() and app is not None:
        address = document_url(app.jwks_url) or next(iter(app.frame_origins()), "")
    candidates = candidate_urls(address)
    if not candidates:
        raise wire.SiteDocumentError("no_address", "give the site's address (https://…), the URL "
                                                   "of its keys, or of its site.json")
    tried: list[str] = []
    token, url, failures = "", "", []
    for candidate in candidates:
        tried.append(candidate)
        try:
            token, url = _get(candidate), candidate
            break
        except sdk_fetch.FetchError as exc:
            failures.append(str(exc))
    if not token:
        raise wire.SiteDocumentError(
            "unreachable", "no site.json at " + "; ".join(failures) + ". Serve the SDK's "
            f"canopy/site.json (dimagi-canopy 0.16+), and mount it at {wire.WELL_KNOWN_PATH} "
            "so the site's address alone finds it — or give the URL of its keys")
    try:
        claims = unverified_claims(token)
        unverified = wire.SiteNeeds.from_claims(claims)
    except wire.SiteDocumentError:
        raise
    except Exception as exc:  # noqa: BLE001 - ContractError: not a token at all
        raise wire.SiteDocumentError("malformed", f"{url} is not a signed site document") from exc
    name = str(claims.get("iss") or "").strip()
    if app is not None and name != app.name:
        raise wire.SiteDocumentError("bad_issuer", f"the document at {url} is {name!r}'s, not "
                                                   f"{app.name!r}'s")
    if unverified.site is None:
        raise wire.SiteDocumentError("no_facts", f"the document at {url} declares none of the "
                                                 "site's own facts — upgrade the site to "
                                                 "dimagi-canopy 0.16, whose site.json fills them")
    registered: bool | None = None
    if app is not None and (app.public_keys or app.jwks_url):
        try:
            registered = _verifies(token, assertions.keys_for_app(app, token), name)
        except assertions.AssertionError_:
            registered = False
    published = False
    if not registered and unverified.site.jwks_url:
        published = _verifies(token, _published_keys(unverified.site.jwks_url, token), name)
    if not (registered or published):
        raise wire.SiteDocumentError(
            "bad_signature", f"the document at {url} is not signed by "
            + ("the keys canopy has registered for this site or " if registered is False else "")
            + "the keys it names" + (f" ({unverified.site.jwks_url})" if unverified.site.jwks_url
                                     else " — it names no jwks_url"))
    return Discovery(url=url, name=name, facts=unverified.site,
                     signed_by="registered_keys" if registered else "published_keys",
                     registered_keys_verify=registered, tried=tuple(tried))


# --- the schemes of the site's own resource links (spec 2026-10-23-host-held-results) ----


def resource_schemes_for(session) -> dict[str, int | None]:
    """The URI schemes the session's Connected site declares for its artifacts
    (`site.json`'s `site.resource_schemes`), each with the exact length of its result ids
    where the site declares one (`site.resource_id_lengths`), else None: a sensitive
    conversation keeps a host's resource link only under one of these, as
    `<scheme>://<id>` (`harness.sensitivity.strip_result`, `canopy_sdk.resources.id_of`).

    Read LIVE from the verified, cached document, with no owner adopting it, unlike
    the rest of the `site` block: it widens nothing — it lists the addresses canopy
    keeps from this site's own tool results. Fails closed: no session site, no
    verifiable document, or no block → no schemes, so every link is dropped."""
    app = site_app(session) if session is not None else None
    if app is None:
        return {}
    document = needs_for(app).document
    facts = document.site if document is not None else None
    return facts.resource_policy() if facts is not None else {}


def is_site_tool(session, tool: str) -> bool:
    """Whether `tool` (a tool call's name as the transcript writes it) is the session's
    Connected site's own: canopy's `site_call`, or one of the site's tools the runner
    reached directly (`mcp__<server>__<tool>`) that canopy has indexed for it. Only these
    tools' resource links are kept in a sensitive conversation: a link-shaped answer from
    anything else (a shell, a file read) is the agent's own words, not the site's
    address. Fails closed: no session site → False."""
    from apps.mcp_apps_host import mcp_apps  # noqa: PLC0415 — as the gateway: loads with a call

    from .host_gateway import host_tool_name  # noqa: PLC0415 — as above

    name = host_tool_name(str(tool or ""))
    if not name:
        return False
    if name == "site_call":
        # canopy's own MCP server (`mcp__plugin_canopy_canopy-web__site_call`), not another
        # server's tool of the same name.
        server = str(tool).split("__")[1] if str(tool).startswith("mcp__") else ""
        return "canopy" in server.lower()
    app = site_app(session) if session is not None else None
    return app is not None and mcp_apps.indexed_tool(app, name) is not None
