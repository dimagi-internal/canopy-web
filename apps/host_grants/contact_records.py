"""A host site keeps canopy's copy of its people current: `POST /api/sites/contacts`.

A host site is the SYSTEM OF RECORD for the people it brings
(docs/architecture/host-systems-of-record.md). It signs the request with its own key —
the one canopy already verifies its visitor assertions with — so no new secret exists.
The wire and its checks are the SDK's (`canopy_sdk.contacts`); here canopy resolves the
site (`site_agents.site_for_request`: its name, and the workspace it names), checks the
signature with that site's registered keys, spends the `jti`, and writes each person's
host-owned fields (`contacts.services.apply_host_record`) in THAT site's workspace,
keyed by THAT site — so a site can only ever write people it vouches for itself.
"""
from __future__ import annotations

from datetime import datetime, timezone

from canopy_sdk import contacts as wire
from canopy_sdk import contract
from django.conf import settings
from django.core.cache import cache

from apps.embedding import assertions
from apps.common.rate_limit import over
from apps.contacts import services
from apps.embedding.rate_limit import SiteAgentsRateLimitError

from . import client_identity, site_agents, site_needs
from .site_agents import SiteAgentsRefused

UPDATE, ERASE = "update", "erase"
_PATHS = {UPDATE: wire.UPDATE_PATH, ERASE: wire.ERASE_PATH}


def audiences(kind: str) -> list[str]:
    """The endpoint URL under canopy's protocol identity and under the address people
    visit (`site_agents.audiences`'s rule)."""
    bases = [client_identity.public_base(),
             (getattr(settings, "CANOPY_PUBLIC_BASE_URL", "") or "").rstrip("/")]
    return list(dict.fromkeys(contract.normalize_url(b) + _PATHS[kind] for b in bases if b))


def check_budget(client_ip, issuer: str = "") -> None:
    """Per client before anything is parsed, per site before its signature is checked.
    A host may sync many people (100 a request), so generous, but bounded."""
    limit = int(getattr(settings, "SITE_CONTACTS_LIMIT", 600))
    window = int(getattr(settings, "SITE_CONTACTS_WINDOW_SECONDS", 60))
    key = f"tokens:site-contacts:iss:{issuer}" if issuer else \
        f"tokens:site-contacts:ip:{client_ip or 'unknown'}"
    if over(key, limit, window):
        raise SiteAgentsRateLimitError(f"too many contact requests ({limit} per {window}s)")


def verify(token: str, kind: str):
    """`(app, claims)` for a request signed by a registered site's own key, or
    `SiteAgentsRefused` (`unknown_issuer`, `bad_signature`, `replayed`, `wrong_audience`,
    `bad_typ`, `too_many`…)."""
    app = site_agents.site_for_request(token)
    try:
        keys = assertions.keys_for_app(app, token)
    except assertions.AssertionError_ as exc:
        raise SiteAgentsRefused(exc.code, exc.message) from exc

    def spend(claims: dict) -> None:
        ttl = max(1, int(claims["exp"]) - int(claims["iat"]) + contract.LEEWAY_SECONDS)
        # Fails CLOSED: a cache that cannot record the jti reads as a replay.
        if not cache.add(f"site-contacts:jti:{app.pk}:{claims['jti']}", 1, timeout=ttl):
            raise wire.ContactRecordError("replayed", "this request has already been used")

    check = wire.verify_update if kind == UPDATE else wire.verify_erase
    try:
        claims = check(token, keys, audiences=audiences(kind), spend_jti=spend,
                       algorithms=assertions.ALLOWED_ALGORITHMS)
    except wire.ContactRecordError as exc:
        status = 400 if exc.code in ("malformed", "bad_alg", "too_many", "bad_sub") else 401
        raise SiteAgentsRefused(exc.code, exc.message, status) from exc
    if claims.get("iss") != app.name:
        raise SiteAgentsRefused("unknown_issuer", f"no host site named {claims.get('iss')!r}")
    return app, claims


def apply_update(app, claims: dict) -> list[dict]:
    """Each item of a verified update, written or refused on its own: one bad item never
    sinks the others. `[{sub, contact_id, status, reason?}]`, in the request's order."""
    scheme = None
    signed_at = datetime.fromtimestamp(int(claims["iat"]), tz=timezone.utc)
    results = []
    for raw in claims["contacts"]:
        sub = raw.get("sub") if isinstance(raw, dict) and isinstance(raw.get("sub"), str) else ""
        if isinstance(raw, dict) and raw.get("record_uri") and scheme is None:
            # Read once, only when an item names a record: no document, no scheme, so a
            # `record_uri` is refused rather than kept under a scheme nobody declared.
            scheme = site_needs.person_scheme_for(app)
        try:
            item = wire.check_item(raw, person_scheme=scheme or "")
        except wire.ContactRecordError as exc:
            results.append({"sub": sub, "contact_id": None, "status": wire.REFUSED,
                            "reason": f"{exc.code}: {exc.message}"})
            continue
        contact, status = services.apply_host_record(app=app, item=item, signed_at=signed_at)
        if contact is None:
            results.append({"sub": item["sub"], "contact_id": None, "status": wire.REFUSED,
                            "reason": "erased: this update was signed before the site erased this person"})
            continue
        results.append({"sub": item["sub"], "contact_id": contact.pk, "status": status})
    return results


def apply_erase(app, claims: dict) -> list[dict]:
    """Each `sub` of a verified erasure: `erased`, or `not_found` when canopy holds no such
    person for THIS site (never erased, or already erased). `[{sub, status}]`."""
    from apps.contacts.models import Contact  # noqa: PLC0415 — as `services`: kept beside its one reader

    results = []
    for sub in claims["subs"]:
        contact = Contact.objects.filter(workspace_id=app.workspace_id, app=app, external_id=sub).first()
        if contact is None:
            results.append({"sub": sub, "status": wire.NOT_FOUND})
            continue
        services.erase(contact, reason="host")
        results.append({"sub": sub, "status": wire.ERASED})
    return results
