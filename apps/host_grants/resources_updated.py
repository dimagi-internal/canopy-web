"""A connected site tells canopy its resources changed (spec
2026-10-23-host-held-results §3): `POST /api/sites/resources-updated`.

The site signs the request with its own key — the one canopy already verifies its
visitor assertions and site agents requests with — so no new secret exists. The
wire and its checks are the SDK's (`canopy_sdk.resources`); here canopy resolves
the site (`site_agents.site_for_request`: its name, and the workspace or agent it
names), checks the signature with that site's registered keys, spends the `jti`,
and relays each URI to the Views subscribed to it on THAT site
(`mcp_apps_views.resources_updated`). A URI is all that travels; each View
re-reads it as its own viewer, under the host's ACL.
"""
from __future__ import annotations

from canopy_sdk import contract
from canopy_sdk import resources as wire
from django.conf import settings
from django.core.cache import cache

from apps.embedding import assertions

from . import client_identity, site_agents
from .site_agents import SiteAgentsRefused


def audiences() -> list[str]:
    """The endpoint URL under canopy's protocol identity and under the address
    people visit (`site_agents.audiences`'s rule)."""
    bases = [client_identity.public_base(),
             (getattr(settings, "CANOPY_PUBLIC_BASE_URL", "") or "").rstrip("/")]
    return list(dict.fromkeys(wire.updated_url(b) for b in bases if b))


def verify(token: str):
    """`(app, claims)` for a notification signed by a registered site's own key, or
    `SiteAgentsRefused` (its codes: `unknown_issuer`, `ambiguous_issuer`,
    `bad_signature`, `replayed`, `wrong_audience`...)."""
    app = site_agents.site_for_request(token)
    try:
        keys = assertions.keys_for_app(app, token)
    except assertions.AssertionError_ as exc:
        raise SiteAgentsRefused(exc.code, exc.message) from exc

    def spend(claims: dict) -> None:
        ttl = max(1, int(claims["exp"]) - int(claims["iat"]) + contract.LEEWAY_SECONDS)
        # Fails CLOSED: a cache that cannot record the jti reads as a replay.
        if not cache.add(f"resources-updated:jti:{app.pk}:{claims['jti']}", 1, timeout=ttl):
            raise wire.ResourceError("replayed", "this notification has already been used")

    try:
        claims = wire.verify_updated_notification(token, keys, audiences=audiences(), spend_jti=spend,
                                                  algorithms=assertions.ALLOWED_ALGORITHMS)
    except wire.ResourceError as exc:
        raise SiteAgentsRefused(exc.code, exc.message,
                                400 if exc.code in ("malformed", "bad_alg") else 401) from exc
    if claims.get("iss") != app.name:
        raise SiteAgentsRefused("unknown_issuer", f"no connected site named {claims.get('iss')!r}")
    return app, claims
