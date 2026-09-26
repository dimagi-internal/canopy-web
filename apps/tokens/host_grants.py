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

import hmac
import logging
from datetime import timedelta
from urllib.parse import urlparse

from django.core.cache import cache
from django.utils import timezone

from . import client_identity, outbound

log = logging.getLogger(__name__)

#: The contract caps an ID-JAG at 300s after `iat`.
MAX_ID_JAG_LIFETIME = 300
LEEWAY_SECONDS = 30
#: The contract caps a host access token at 900s; a longer claim is clamped,
#: never trusted — a host bug must not make a visitor's token outlive the visit.
MAX_TOKEN_SECONDS = 900
ID_JAG_TYP = "oauth-id-jag+jwt"
#: What canopy accepts a host to sign with. The contract: EdDSA or ES256.
ID_JAG_ALGORITHMS = ["EdDSA", "ES256"]
METADATA_CACHE_SECONDS = 600
#: An ID-JAG is a handful of claims; anything this large is not one, and is
#: refused before a signature check spends CPU on it.
MAX_ID_JAG_BYTES = 8192


class HostGrantError(Exception):
    """A refusal with a stable `code`. The message never contains a credential."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _eq(a: str, b: str) -> bool:
    """Constant-time equality for identifiers compared against a claim."""
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def _norm(url: str) -> str:
    return (url or "").strip().rstrip("/")


# --- discovery ---------------------------------------------------------------


def metadata_url(issuer: str) -> str:
    """RFC 8414 §3.1: insert the well-known segment between host and path."""
    parsed = urlparse(_norm(issuer))
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-authorization-server{path}"


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
    if not _eq(_norm(str(doc.get("issuer") or "")), issuer):
        raise HostGrantError("issuer_mismatch",
                             f"the metadata at {metadata_url(issuer)} names a different issuer")
    token_endpoint = str(doc.get("token_endpoint") or "")
    try:
        outbound.check_url(token_endpoint, what="token endpoint")
    except outbound.OutboundError as exc:
        raise HostGrantError("bad_token_endpoint", str(exc)) from exc
    out = {"issuer": issuer, "token_endpoint": token_endpoint}
    cache.set(key, out, METADATA_CACHE_SECONDS)
    return out


# --- the ID-JAG ----------------------------------------------------------------


def check_id_jag(app, id_jag: str, *, subject: str) -> dict:
    """Verify the host's grant before canopy spends it. Returns its claims."""
    import jwt

    from . import assertions

    try:
        header = jwt.get_unverified_header(id_jag)
    except Exception as exc:  # noqa: BLE001
        raise HostGrantError("malformed", "the id_jag is not a readable JWT") from exc
    if header.get("typ") != ID_JAG_TYP:
        raise HostGrantError("wrong_type", f"an id_jag must have typ {ID_JAG_TYP!r}")
    try:
        keys = assertions.keys_for_app(app, id_jag)
    except assertions.AssertionError_ as exc:
        raise HostGrantError(exc.code, exc.message) from exc

    issuer = _norm(app.host_issuer)
    claims = None
    last: Exception | None = None
    for key in keys:
        try:
            claims = jwt.decode(
                id_jag, key,
                algorithms=ID_JAG_ALGORITHMS,      # ours, never the header's
                audience=[issuer, issuer + "/"],
                leeway=LEEWAY_SECONDS,
                options={"require": ["iss", "sub", "aud", "exp", "iat", "jti",
                                     "client_id", "resource"]},
            )
            break
        except jwt.InvalidAudienceError as exc:
            raise HostGrantError("wrong_audience", "the id_jag is not addressed to the "
                                 "site's own authorization server") from exc
        except jwt.ExpiredSignatureError as exc:
            raise HostGrantError("expired", "the id_jag has expired") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise HostGrantError("incomplete", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - try the next key
            last = exc
    if claims is None:
        raise HostGrantError("bad_signature",
                             f"no key registered for {app.name!r} verifies the id_jag "
                             f"({type(last).__name__ if last else 'no key'})")

    if not _eq(_norm(str(claims["iss"])), issuer):
        raise HostGrantError("wrong_issuer", "the id_jag was not issued by the site's issuer")
    if not _eq(str(claims["client_id"]), client_identity.client_id()):
        raise HostGrantError("wrong_client", "the id_jag is for a different client")
    if not _eq(_norm(str(claims["resource"])), _norm(app.host_mcp_resource)):
        raise HostGrantError("wrong_resource",
                             "the id_jag names a resource other than the site's MCP server")
    if not _eq(str(claims["sub"]), subject):
        raise HostGrantError("wrong_subject",
                             "the id_jag names a different person than the arrival assertion")
    lifetime = int(claims["exp"]) - int(claims["iat"])
    if lifetime > MAX_ID_JAG_LIFETIME + LEEWAY_SECONDS:
        raise HostGrantError("too_long", f"an id_jag may live at most {MAX_ID_JAG_LIFETIME}s")
    return claims


# --- redemption ------------------------------------------------------------------


def _token_request(endpoint: str, form: dict):
    """One POST, retried ONCE if the host demands a DPoP nonce (RFC 9449 §8)."""
    nonce = None
    for _ in range(2):
        # A fresh client assertion AND proof per attempt: both are single-use.
        body = {**form,
                "client_assertion": client_identity.client_assertion(form["_aud"])}
        body.pop("_aud")
        proof = client_identity.dpop_proof("POST", endpoint, nonce=nonce)
        status, doc, headers = outbound.post_form(endpoint, body, headers={"DPoP": proof},
                                                  what="token endpoint")
        if status == 400 and doc.get("error") == "use_dpop_nonce" and headers.get("DPoP-Nonce") \
                and nonce is None:
            nonce = headers["DPoP-Nonce"]
            continue
        return status, doc
    return status, doc


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
            "grant_type": client_identity.JWT_BEARER_GRANT,
            "assertion": id_jag,
            "client_id": client_identity.client_id(),
            "client_assertion_type": client_identity.CLIENT_ASSERTION_TYPE,
            "resource": app.host_mcp_resource,
        })
    except outbound.OutboundError as exc:
        raise HostGrantError("token_endpoint_unreachable", str(exc)) from exc
    if status != 200:
        # The OAuth error CODE only — `error_description` is the host's prose
        # and could echo anything back.
        raise HostGrantError("redeem_refused",
                             f"the host refused the grant ({status} {doc.get('error') or 'error'})")
    token = doc.get("access_token")
    if not isinstance(token, str) or not token:
        raise HostGrantError("bad_response", "the host returned no access_token")
    if str(doc.get("token_type") or "").lower() != "dpop":
        # A bearer token here would be usable by anyone who copied it; the
        # contract says DPoP, so anything else is refused rather than stored.
        raise HostGrantError("not_dpop", "the host returned a token that is not DPoP-bound")
    try:
        lifetime = int(doc.get("expires_in") or 0)
    except (TypeError, ValueError):
        lifetime = 0
    if lifetime <= 0:
        raise HostGrantError("bad_response", "the host returned no expires_in")
    lifetime = min(lifetime, MAX_TOKEN_SECONDS)
    scope = str(doc.get("scope") or claims.get("scope") or "")[:500]

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
