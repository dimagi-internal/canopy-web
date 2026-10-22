"""What a connected site asks of canopy's agents (its signed `site.json`), and
whether each agent canopy allows there meets it (spec connect-labs
`2026-10-22-canopy-site-offer-design` §2).

The site publishes `site.json` beside its `jwks.json` (the SDK's host route), so
canopy needs no new field to find it: `canopy_sdk.site.site_document_url`. It
is fetched the way the site's keys are — https only, address-pinned, no
redirects, bounded (`canopy_sdk.fetch`) — cached, and FAIL-CLOSED: no
verifiable document is shown as unavailable with its reason, never as "meets".
A copy is reused for `FRESH_SECONDS`; when a refetch fails the last good copy
is shown marked stale for up to `STALE_SECONDS`.

It is verified with the site's registered keys (`assertions.keys_for_app`, the
same that verify its visitor assertions), and `iss` must be the site's name.

`fit` compares it with canopy's own offer for the site (`site_offer.
offer_content`), so the Connected sites page and the offer the site reads can
never disagree about an agent.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from canopy_sdk import fetch as sdk_fetch
from canopy_sdk import site as wire
from django.core.cache import cache

from apps.embedding import assertions

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


def needs_for(app, *, now: float | None = None) -> Needs:
    t = time.time() if now is None else now
    url = wire.site_document_url(app.jwks_url)
    if not url:
        return Needs(url="", document=None, reason=(
            "this site publishes no keys at a jwks.json URL, so canopy has nowhere to read its "
            "site.json — give it the SDK's canopy/jwks.json URL"))
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


def fit(document: wire.SiteNeeds, content: dict) -> tuple[list[dict], list[str]]:
    """Per allowed agent: does the site offer it, let it read real data, and does
    it meet `real_data.requires` — and if not, why. Plus the agents the site
    names that canopy does not allow there (they can never appear)."""
    requires = dict(document.real_data_requires or {})
    flag = requires.get("runner_flag") or ""
    rows = []
    for agent in content["agents"]:
        why: list[str] = []
        if requires.get("phi_safe") and not any(c["phi_safe"] for c in agent["capabilities"]):
            why.append(f"not PHI-safe in canopy: no capability of {agent['name']} for this site "
                       "says `data: phi`")
        if flag and not agent["runners"]["ready_with"].get(flag):
            # The offer's own sentence when it has one: it names where to fix it.
            why.append(next((p for p in agent["problems"]
                             if p.startswith(f"no ready runner with {flag}")),
                            f"no ready runner with {flag}"))
        why += [p for p in agent["problems"] if p not in why]
        rows.append({
            "slug": agent["slug"],
            "name": agent["name"],
            "offered": agent["slug"] in document.uses_agents,
            "real_data": agent["slug"] in document.uses_real_data_agents,
            "ready": agent["ready"],
            "meets_real_data": agent["ready"] and not why,
            "why_not": why,
        })
    allowed = {a["slug"] for a in content["agents"]}
    named = list(dict.fromkeys([*document.uses_agents, *document.uses_real_data_agents]))
    return rows, [s for s in named if s not in allowed]
