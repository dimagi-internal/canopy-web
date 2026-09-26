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

import base64
import hashlib
import json
import threading
import time
import uuid

from django.conf import settings

#: The client-assertion lifetime (contract: `exp` <= 60s). It is used once, at
#: the moment of the POST.
CLIENT_ASSERTION_TTL_SECONDS = 60

CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

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
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519

    if isinstance(key, ed25519.Ed25519PrivateKey):
        return
    if isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name == "secp256r1":
        return
    raise ClientIdentityError(
        f"{name} must be an Ed25519 or P-256 private key — nothing symmetric, "
        "nothing RSA: the contract allows EdDSA and ES256 only"
    )


def _alg(key) -> str:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    if isinstance(key, (ed25519.Ed25519PrivateKey, ed25519.Ed25519PublicKey)):
        return "EdDSA"
    return "ES256"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def public_jwk(public_key) -> dict:
    """The public half as a JWK with an RFC 7638 thumbprint as its `kid`.

    Only public members: this is published, and a `d` member here would publish
    the ability to sign.
    """
    from jwt.algorithms import ECAlgorithm, OKPAlgorithm
    from cryptography.hazmat.primitives.asymmetric import ed25519

    if isinstance(public_key, ed25519.Ed25519PublicKey):
        jwk = OKPAlgorithm.to_jwk(public_key, as_dict=True)
    else:
        jwk = ECAlgorithm.to_jwk(public_key, as_dict=True)
    jwk = {k: v for k, v in jwk.items() if k != "d"}
    jwk["kid"] = thumbprint(jwk)
    jwk["alg"] = _alg(public_key)
    return jwk


def thumbprint(jwk: dict) -> str:
    """RFC 7638: SHA-256 over the REQUIRED members only, sorted, no whitespace.

    It is both the `kid` canopy publishes and the `jkt` a host binds a DPoP
    token to (`cnf.jkt`), so it must be the value any other library computes.
    """
    members = ("crv", "kty", "x") if jwk.get("kty") == "OKP" else ("crv", "kty", "x", "y")
    canonical = json.dumps({m: jwk[m] for m in members}, separators=(",", ":"), sort_keys=True)
    return _b64(hashlib.sha256(canonical.encode()).digest())


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
    return {
        "client_id": client_id(),
        "client_name": "canopy",
        "jwks_uri": jwks_uri(),
        "token_endpoint_auth_method": "private_key_jwt",
        "grant_types": [JWT_BEARER_GRANT],
        "dpop_bound_access_tokens": True,
    }


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
    import jwt

    key = _client_key()
    now = int(time.time())
    claims = {
        "iss": client_id(),
        "sub": client_id(),
        "aud": audience,
        "iat": now,
        "exp": now + CLIENT_ASSERTION_TTL_SECONDS,
        "jti": str(uuid.uuid4()),
    }
    kid = public_jwk(key.public_key())["kid"]
    return jwt.encode(claims, key, algorithm=_alg(key), headers={"kid": kid})


def dpop_jkt() -> str:
    """The thumbprint a DPoP-bound token is bound to (`cnf.jkt`)."""
    return public_jwk(_dpop_key().public_key())["kid"]


def dpop_proof(htm: str, htu: str, *, access_token: str | None = None,
               nonce: str | None = None) -> str:
    """A fresh RFC 9449 proof for ONE request.

    `htu` is the target URI without query or fragment. `ath` binds the proof to
    the access token it accompanies, so a proof lifted from one call cannot
    carry a different token.
    """
    import jwt

    key = _dpop_key()
    jwk = public_jwk(key.public_key())
    header_jwk = {k: v for k, v in jwk.items() if k not in ("kid", "alg")}
    claims: dict = {
        "jti": str(uuid.uuid4()),
        "htm": htm.upper(),
        "htu": htu,
        "iat": int(time.time()),
    }
    if access_token is not None:
        claims["ath"] = _b64(hashlib.sha256(access_token.encode("ascii")).digest())
    if nonce:
        claims["nonce"] = nonce
    return jwt.encode(claims, key, algorithm=_alg(key),
                      headers={"typ": "dpop+jwt", "jwk": header_jwk})


def _reset_for_tests() -> None:
    """Drop the ephemeral keys, e.g. to prove rotation invalidates a grant."""
    with _lock:
        _ephemeral.clear()
