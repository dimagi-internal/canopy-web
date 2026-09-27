"""Redeeming a host-issued grant for one visitor (host grant contract v1, §1–§2).

At arrival a host that signed its visitor in may send, beside the usual signed
assertion, an **ID-JAG** (`draft-ietf-oauth-identity-assertion-authz-grant`): a
short JWT, signed by the host's own key, saying "canopy may get a token for MY
MCP server, as this visitor, with this page's scope". canopy redeems it here:

1. **Check it before spending anything.** Signature against the site's own keys
   (the same ones the arrival assertion verifies with), `typ`, `iss` = `aud` =
   the site's issuer, `client_id` = canopy, `resource` = the site's MCP URL,
   `sub` = the arrival assertion's `sub`, and a lifetime of at most 300s. The
   host checks all of this again; canopy checking first means a grant naming
   somebody else, or another resource, is never even sent.
2. **Discover** the host's token endpoint from its RFC 8414 metadata, and
   refuse unless the document's `issuer` is the configured one (RFC 9207's
   concern: a metadata document is only as good as the issuer it names).
3. **Redeem** with the RFC 7523 jwt-bearer grant, authenticating with
   `private_key_jwt`, sender-constrained with a DPoP proof, naming the
   `resource` (RFC 8707).
4. **Store** the access token encrypted, bound to (site, the visitor's host id).

**Everything here fails closed, and none of it fails the arrival.** The caller
(`contact_api.contact_token`) catches `HostGrantError`, records an Event and
carries on: the chat still opens, the agent simply has no way to reach the host
as this visitor — which is exactly the state before this existed, minus the
agent's own credential (a visitor turn never falls back to it).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from canopy_sdk import consumer, contract
from django.core.cache import cache
from django.utils import timezone

from . import client_identity, outbound

log = logging.getLogger(__name__)

# The contract's values and checks live in the SDK (`canopy_sdk.contract` /
# `canopy_sdk.consumer`), which hosts build against too. Re-exported under the
# names this module has always had.

#: The contract caps an ID-JAG at 300s after `iat`.
MAX_ID_JAG_LIFETIME = contract.ID_JAG_MAX_LIFETIME
LEEWAY_SECONDS = contract.LEEWAY_SECONDS
#: The contract caps a host access token at 900s; a longer claim is clamped,
#: never trusted — a host bug must not make a visitor's token outlive the visit.
MAX_TOKEN_SECONDS = contract.ACCESS_TOKEN_MAX_LIFETIME
ID_JAG_TYP = contract.ID_JAG_TYP
#: What canopy accepts a host to sign with. The contract: EdDSA or ES256.
ID_JAG_ALGORITHMS = list(contract.GRANT_ALGORITHMS)
METADATA_CACHE_SECONDS = 600
#: An ID-JAG is a handful of claims; anything this large is not one, and is
#: refused before a signature check spends CPU on it.
MAX_ID_JAG_BYTES = contract.MAX_JWT_BYTES

#: A refusal with a stable `code`. The message never contains a credential. The
#: SDK's class, so a refusal from `consumer.check_id_jag` is this type.
HostGrantError = consumer.RedemptionRefused


def _norm(url: str) -> str:
    return contract.normalize_url(url)


# --- discovery ---------------------------------------------------------------


def metadata_url(issuer: str) -> str:
    """RFC 8414 §3.1: insert the well-known segment between host and path."""
    return contract.metadata_url(issuer)


def discover(issuer: str) -> dict:
    """The host's authorization-server metadata, validated. Cached.

    The `issuer` in the document must be the one configured on the site, and
    the token endpoint it names must itself pass the outbound checks — a
    metadata document is a list of URLs canopy is about to send credentials to.
    """
    issuer = _norm(issuer)
    key = f"tokens:host-as:{issuer}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        doc = outbound.get_json(metadata_url(issuer), what="host metadata URL")
    except outbound.OutboundError as exc:
        raise HostGrantError("metadata_unreachable", str(exc)) from exc
    token_endpoint = consumer.validate_authorization_server_metadata(doc, issuer)
    try:
        outbound.check_url(token_endpoint, what="token endpoint")
    except outbound.OutboundError as exc:
        raise HostGrantError("bad_token_endpoint", str(exc)) from exc
    out = {"issuer": issuer, "token_endpoint": token_endpoint}
    cache.set(key, out, METADATA_CACHE_SECONDS)
    return out


# --- the ID-JAG ----------------------------------------------------------------


def check_id_jag(app, id_jag: str, *, subject: str) -> dict:
    """Verify the host's grant before canopy spends it. Returns its claims.

    The checks are the SDK's `consumer.check_id_jag`; which keys a site has
    registered (its pasted PEMs + published JWKS) is ours, resolved only once
    the header says this is an ID-JAG at all.
    """
    from . import assertions

    def keys():
        try:
            return assertions.keys_for_app(app, id_jag)
        except assertions.AssertionError_ as exc:
            raise HostGrantError(exc.code, exc.message) from exc

    return consumer.check_id_jag(
        id_jag, keys,
        issuer=app.host_issuer,
        client_id=client_identity.client_id(),
        resource=app.host_mcp_resource,
        subject=subject,
        label=app.name,
        algorithms=ID_JAG_ALGORITHMS,
        leeway=LEEWAY_SECONDS,
        max_lifetime=MAX_ID_JAG_LIFETIME,
    )


# --- redemption ------------------------------------------------------------------


def _token_request(endpoint: str, form: dict):
    """One POST, retried ONCE if the host demands a DPoP nonce (RFC 9449 §8).

    `outbound.post_form` is looked up per call (not bound at import) so the
    transport stays the one guarded door out — and replaceable in tests.
    """
    audience = form["_aud"]
    body = {k: v for k, v in form.items() if k != "_aud"}
    return consumer.request_token(
        lambda url, data, headers, what="": outbound.post_form(url, data, headers=headers, what=what),
        endpoint, body, audience=audience,
        client_assertion=client_identity.client_assertion,
        dpop_proof=lambda htm, htu, nonce=None: client_identity.dpop_proof(htm, htu, nonce=nonce),
    )


def redeem(app, id_jag: str, *, subject: str, contact=None, user=None):
    """Redeem the host's ID-JAG for a DPoP-bound access token and store it.

    Returns the `HostGrant`. Raises `HostGrantError` for every refusal — the
    caller decides that a refusal does not fail the arrival.
    """
    from apps.common.encryption import encrypt_secret

    from .models import HostGrant

    if not app.issues_host_grants():
        raise HostGrantError("not_configured",
                             f"{app.name!r} has no host issuer / MCP resource configured")
    if not client_identity.configured():
        raise HostGrantError("no_client_key", "this canopy has no OAuth client keys")

    claims = check_id_jag(app, id_jag, subject=subject)
    meta = discover(app.host_issuer)
    try:
        status, doc = _token_request(meta["token_endpoint"], {
            "_aud": meta["issuer"],
            **consumer.redemption_form(id_jag, client_id=client_identity.client_id(),
                                       resource=app.host_mcp_resource),
        })
    except outbound.OutboundError as exc:
        raise HostGrantError("token_endpoint_unreachable", str(exc)) from exc
    # The OAuth error CODE only (`error_description` is the host's prose and
    # could echo anything back); DPoP or nothing (a bearer token here would be
    # usable by anyone who copied it); `expires_in` clamped, never trusted.
    answer = consumer.parse_token_response(status, doc, max_lifetime=MAX_TOKEN_SECONDS)
    token, lifetime = answer.access_token, answer.expires_in
    scope = str(answer.scope or claims.get("scope") or "")[:500]

    grant, _ = HostGrant.objects.update_or_create(
        app=app, subject=subject,
        defaults={
            "contact": contact,
            "user": user,
            "access_token_enc": encrypt_secret(token),
            "scope": scope,
            "resource": app.host_mcp_resource,
            "dpop_jkt": client_identity.dpop_jkt(),
            "expires_at": timezone.now() + timedelta(seconds=lifetime),
        },
    )
    return grant


def record_outcome(app, *, ok: bool, subject: str, code: str = "", detail: str = "",
                   scope: str = "") -> None:
    """One Event per redemption — the place an operator looks when "the agent
    could not reach the host" and nothing else says why. Best-effort."""
    try:
        from apps.events import services as events

        events.record([{
            "source": "tokens.host_grants",
            "kind": "host_grant.redeemed" if ok else "host_grant.refused",
            "level": "info" if ok else "warn",
            # Coalesce failures per site+code so a misconfigured host is one
            # row with a count, not a row per page view.
            "key": "" if ok else f"{app.pk}:{code}",
            "summary": (f"{app.name}: host grant redeemed (scope {scope or '-'})" if ok
                        else f"{app.name}: host grant refused — {code}"),
            "payload": {"site": app.name, "subject": subject, "code": code,
                        "detail": detail[:300], "scope": scope},
        }], workspace=app.workspace)
    except Exception:  # noqa: BLE001 - bookkeeping must never fail an arrival
        log.exception("could not record a host grant event")
