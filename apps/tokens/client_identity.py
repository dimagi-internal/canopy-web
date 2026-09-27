"""canopy as an OAuth CLIENT of a host site — its identity, and nothing that can
assert a user.

Host grant contract v1 (`docs/architecture/host-grant-contract.md`). When a
visitor on a host page talks to an embedded agent, the HOST (which signed the
visitor in) issues a short grant for its own MCP server; canopy redeems it and
calls the host's tools as that visitor. This module is canopy's half of the
handshake that makes the redemption trustworthy:

* **Client ID Metadata Document (CIMD).** canopy's `client_id` IS a URL,
  `{CANOPY_PUBLIC_BASE_URL}/oauth/client.json`, serving the document below. A
  host allowlists that one URL and learns everything else from it — the MCP
  2026-07-28 replacement for dynamic client registration, and the reason no key
  is ever pasted between systems.
* **The client key** signs the `private_key_jwt` client assertion (RFC 7523)
  that authenticates canopy at the host's token endpoint. Its public half is
  published at `/oauth/jwks.json`.
* **The DPoP key** (RFC 9449) sender-constrains the access tokens a host
  issues: a token copied out of a log is useless without a fresh proof signed
  by this key. It is a DIFFERENT key from the client key, so a leak of one does
  not do the other's job, and it is never published — a host learns it from
  the `jwk` header of each proof and binds the token to its thumbprint.

**What neither key can do is the point.** The canopy-minted on-behalf-of
assertion this replaces had canopy sign "this is Gillian" with a key a host
trusted — whoever held it could sign for anyone at every connected site. Here a
host trusts canopy's keys only to say "this request is canopy", and a grant
naming a user is signed by the HOST's key, which never leaves the host.

Asymmetric only: EdDSA (Ed25519) or ES256 (P-256), chosen by the key's type.
Keys come from settings (Secrets Manager in a deployment); "PLACEHOLDER" and
empty read as UNCONFIGURED, which is a real state — the deployment is simply not
a client of any host. In dev and tests an ephemeral key is generated per
process instead (`CANOPY_OAUTH_EPHEMERAL_KEYS`), never in a deployment.
"""
from __future__ import annotations

import threading

from canopy_sdk import consumer, contract, keys
from django.conf import settings

# The wire values and the signing itself are the SDK's (`canopy_sdk.contract`,
# `canopy_sdk.consumer`, `canopy_sdk.keys`) — the same definitions a host
# verifies against. This module owns only WHERE canopy's keys come from.

#: The client-assertion lifetime (contract: `exp` <= 60s). It is used once, at
#: the moment of the POST.
CLIENT_ASSERTION_TTL_SECONDS = contract.CLIENT_ASSERTION_MAX_LIFETIME

CLIENT_ASSERTION_TYPE = contract.CLIENT_ASSERTION_TYPE
JWT_BEARER_GRANT = contract.JWT_BEARER_GRANT

_lock = threading.Lock()
_ephemeral: dict[str, object] = {}


class ClientIdentityError(Exception):
    """canopy cannot act as an OAuth client here. The message is for an operator."""


def _pem_setting(name: str) -> str:
    value = (getattr(settings, name, "") or "").strip()
    return "" if value == "PLACEHOLDER" else value


def _load_private(name: str):
    """The private key behind setting `name`, or None when unconfigured.

    An ephemeral key is generated ONCE per process per setting when allowed —
    in dev and tests, where nothing outside the process ever caches it.
    """
    from cryptography.hazmat.primitives import serialization

    pem = _pem_setting(name)
    if pem:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        _check_type(key, name)
        return key
    if not getattr(settings, "CANOPY_OAUTH_EPHEMERAL_KEYS", False):
        return None
    with _lock:
        if name not in _ephemeral:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            _ephemeral[name] = Ed25519PrivateKey.generate()
        return _ephemeral[name]


def _check_type(key, name: str) -> None:
    try:
        keys.check_signing_key(key, what=name)
    except contract.ContractError as exc:
        raise ClientIdentityError(exc.message) from exc


def public_jwk(public_key) -> dict:
    """The public half as a JWK with an RFC 7638 thumbprint as its `kid`.

    Only public members: this is published, and a `d` member here would publish
    the ability to sign.
    """
    return keys.public_jwk(public_key)


def thumbprint(jwk: dict) -> str:
    """RFC 7638: SHA-256 over the REQUIRED members only, sorted, no whitespace.

    It is both the `kid` canopy publishes and the `jkt` a host binds a DPoP
    token to (`cnf.jkt`), so it must be the value any other library computes —
    which is why it is the SDK's, shared with every host.
    """
    return contract.jwk_thumbprint(jwk)


# --- identity -----------------------------------------------------------------


def public_base() -> str:
    return (getattr(settings, "CANOPY_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")


def client_id() -> str:
    """canopy's OAuth client_id: the URL of its own metadata document (CIMD)."""
    return f"{public_base()}/oauth/client.json"


def jwks_uri() -> str:
    return f"{public_base()}/oauth/jwks.json"


def configured() -> bool:
    """Whether canopy can act as a client at all: both keys present."""
    try:
        return _load_private("CANOPY_OAUTH_CLIENT_KEY") is not None and \
            _load_private("CANOPY_OAUTH_DPOP_KEY") is not None
    except Exception:  # noqa: BLE001 - a malformed key is "not configured", loudly elsewhere
        return False


def _client_key():
    key = _load_private("CANOPY_OAUTH_CLIENT_KEY")
    if key is None:
        raise ClientIdentityError("this canopy has no OAuth client key (CANOPY_OAUTH_CLIENT_KEY)")
    return key


def _dpop_key():
    key = _load_private("CANOPY_OAUTH_DPOP_KEY")
    if key is None:
        raise ClientIdentityError("this canopy has no DPoP key (CANOPY_OAUTH_DPOP_KEY)")
    return key


def client_metadata() -> dict:
    """The Client ID Metadata Document, exactly the contract's shape."""
    return contract.client_metadata_document(client_id(), jwks_uri())


def _retired_public() -> list:
    from cryptography.hazmat.primitives import serialization

    raw = getattr(settings, "CANOPY_OAUTH_CLIENT_RETIRED_PUBLIC_KEYS", "") or ""
    out = []
    for pem in (p.strip() for p in raw.split("|")):
        if not pem or pem == "PLACEHOLDER":
            continue
        try:
            out.append(serialization.load_pem_public_key(pem.encode()))
        except Exception:  # noqa: BLE001 - a bad retired key must not break the live one
            continue
    return out


def published_jwks() -> dict:
    """The CLIENT key's public half (plus retired halves still in their
    rollover window). The DPoP key is deliberately absent: it is not what
    authenticates canopy, and a host learns it from each proof."""
    active = public_jwk(_client_key().public_key())
    keys = [{**active, "use": "sig"}]
    seen = {active["kid"]}
    for pub in _retired_public():
        jwk = public_jwk(pub)
        if jwk["kid"] not in seen:
            seen.add(jwk["kid"])
            keys.append({**jwk, "use": "sig"})
    return {"keys": keys}


# --- what canopy signs --------------------------------------------------------


def client_assertion(audience: str) -> str:
    """RFC 7523 `private_key_jwt`: iss = sub = client_id, aud = the host's
    issuer, exp <= 60s, single-use jti."""
    return consumer.client_assertion(_client_key(), client_id(), audience)


def credentials() -> consumer.ClientCredentials:
    """canopy's client identity as the SDK's `ClientCredentials` — for the
    conformance checks a Connected site's "Test connection" runs. Raises
    `ClientIdentityError` when unconfigured."""
    return consumer.ClientCredentials(client_id(), _client_key(), _dpop_key())


def dpop_jkt() -> str:
    """The thumbprint a DPoP-bound token is bound to (`cnf.jkt`)."""
    return consumer.dpop_jkt(_dpop_key())


def dpop_proof(htm: str, htu: str, *, access_token: str | None = None,
               nonce: str | None = None) -> str:
    """A fresh RFC 9449 proof for ONE request.

    `htu` is the target URI without query or fragment. `ath` binds the proof to
    the access token it accompanies, so a proof lifted from one call cannot
    carry a different token.
    """
    return consumer.dpop_proof(_dpop_key(), htm, htu, access_token=access_token, nonce=nonce)


def _reset_for_tests() -> None:
    """Drop the ephemeral keys, e.g. to prove rotation invalidates a grant."""
    with _lock:
        _ephemeral.clear()
