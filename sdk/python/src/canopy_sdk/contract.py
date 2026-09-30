"""The host grant contract, defined ONCE.

Everything the two sides of an embedded canopy agent must agree on lives here:
the version, the token types, the claims each JWT must carry, the lifetime caps,
the algorithms, the DPoP proof rules, and the small pure helpers that compute a
value both sides must get byte-for-byte identical (a JWK thumbprint, an ``ath``,
an RFC 8414 metadata URL).

Before this module, canopy-web (the redeemer) and connect-labs (the first host)
each held their own copy — and they already disagreed in small ways: which
claims an ID-JAG must carry, whether a thumbprint of a malformed key raises.
``canopy_sdk.host`` and ``canopy_sdk.consumer`` both import from here, so a
change to the contract is a change to one file, and the round-trip test in
canopy-web's CI runs the two halves against each other.

Nothing here does I/O and nothing here reads settings.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

# --- version -------------------------------------------------------------------

#: The wire contract this package implements. Bumped only for a change a
#: deployed peer would notice; additive, optional fields do not bump it.
CONTRACT_VERSION = "1"

# --- token and grant types -------------------------------------------------------

#: The ``typ`` header of an Identity Assertion JWT Authorization Grant
#: (draft-ietf-oauth-identity-assertion-authz-grant).
ID_JAG_TYP = "oauth-id-jag+jwt"
#: The ``typ`` header of a DPoP proof (RFC 9449 §4.2).
DPOP_TYP = "dpop+jwt"
#: RFC 7523 §2.1 — the grant canopy redeems an ID-JAG with.
JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
#: RFC 7523 §2.2 — ``private_key_jwt`` client authentication.
CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
#: The only ``token_type`` a host may answer with. A bearer token here would be
#: usable by anyone who copied it.
TOKEN_TYPE_DPOP = "DPoP"
TOKEN_ENDPOINT_AUTH_METHOD = "private_key_jwt"

# --- algorithms --------------------------------------------------------------------

#: What a host may sign the VISITOR ASSERTION with. Asymmetric only: with a
#: symmetric algorithm the verification key is the signing key, so publishing a
#: "public" key would publish the ability to sign. RS256 is accepted here (and
#: only here) because early hosts registered RSA keys for the assertion.
ASSERTION_ALGORITHMS: tuple[str, ...] = ("EdDSA", "ES256", "RS256")
#: What every OTHER JWT in the contract may use: the ID-JAG, canopy's client
#: assertion, and every DPoP proof. Never RSA, never HMAC.
GRANT_ALGORITHMS: tuple[str, ...] = ("EdDSA", "ES256")

#: Key type and curve each grant algorithm needs, so a proof cannot pair
#: ``alg: ES256`` with an Ed25519 key (or the reverse) and slip through a
#: library's key coercion.
KEY_SHAPES: dict[str, tuple[str, str]] = {
    "EdDSA": ("OKP", "Ed25519"),
    "ES256": ("EC", "P-256"),
}
#: The private members of a JWK. A "public" JWK carrying one is a key leak at
#: best and a confused sender at worst; refused either way.
PRIVATE_JWK_MEMBERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "k"})

# --- lifetimes (seconds) ---------------------------------------------------------------

#: The visitor assertion: used once, at the start of a session.
ASSERTION_MAX_LIFETIME = 120
#: The ID-JAG: redeemed immediately, so this only has to cover the hop.
ID_JAG_MAX_LIFETIME = 300
#: canopy's ``private_key_jwt`` client assertion.
CLIENT_ASSERTION_MAX_LIFETIME = 60
#: A host-issued access token. A longer ``expires_in`` is clamped by canopy,
#: never trusted — a host bug must not make a visitor's token outlive the visit.
ACCESS_TOKEN_MAX_LIFETIME = 900
#: Clock skew tolerated on ``exp`` / ``iat`` of the assertion, ID-JAG and client
#: assertion, both directions.
LEEWAY_SECONDS = 30
#: A DPoP proof is fresh by construction (one per request); its ``iat`` must be
#: within this many seconds of the verifier's clock, either side.
DPOP_IAT_WINDOW_SECONDS = 60
#: How long a host may cache canopy's client metadata document and JWKS.
CLIENT_METADATA_MAX_CACHE_SECONDS = 3600

# --- sizes -------------------------------------------------------------------------------

#: A compact JWS in this contract is a handful of claims; anything this large is
#: not one, and is refused before a signature check spends CPU on it.
MAX_JWT_BYTES = 8 * 1024
#: A ``jti`` is an opaque unique string (usually a UUID). Bounded so a hostile
#: one cannot be arbitrarily large.
MAX_JTI_LENGTH = 256

# --- claims ---------------------------------------------------------------------------------

ASSERTION_REQUIRED_CLAIMS: tuple[str, ...] = ("iss", "sub", "aud", "exp", "iat", "jti")
#: What a host may require of the runner a visitor's conversation runs on. A
#: LIST, so a later requirement is a new flag rather than a new claim.
RUNNER_REQUIREMENTS_CLAIM = "canopy_runner_requirements"
#: Every flag a runner's owner may declare and a host may require. The ONLY list:
#: canopy-web imports it rather than keeping a copy.
RUNNER_FLAGS: frozenset[str] = frozenset({"zdr"})


def parse_runner_requirements(value) -> tuple[str, ...]:
    """The claim's value as a sorted, deduped tuple. ``None`` / ``[]`` -> ``()``.

    Raises ``ValueError`` for anything else: a string, a non-string member, an
    unknown flag. Both halves fail CLOSED on it: a host at boot, canopy at arrival.
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{RUNNER_REQUIREMENTS_CLAIM} must be a list of strings")
    unknown = set(value) - RUNNER_FLAGS
    if unknown:
        raise ValueError(f"unknown runner requirement(s): {', '.join(sorted(unknown))}")
    return tuple(sorted(set(value)))

#: What canopy requires before it spends an ID-JAG. ``scope`` is carried too, but
#: the SCOPE is the host's to decide, so it is the host that requires it.
ID_JAG_REQUIRED_CLAIMS: tuple[str, ...] = (
    "iss", "sub", "aud", "exp", "iat", "jti", "client_id", "resource")
HOST_ID_JAG_REQUIRED_CLAIMS: tuple[str, ...] = ID_JAG_REQUIRED_CLAIMS + ("scope",)
CLIENT_ASSERTION_REQUIRED_CLAIMS: tuple[str, ...] = ("iss", "sub", "aud", "exp", "iat", "jti")
DPOP_REQUIRED_CLAIMS: tuple[str, ...] = ("iat", "jti", "htm", "htu")

# --- HTTP ------------------------------------------------------------------------------------

DPOP_HEADER = "DPoP"
DPOP_NONCE_HEADER = "DPoP-Nonce"
#: Informational header naming the acting agent. The host logs it; it is never
#: trusted for authorization.
ACTOR_HEADER = "Canopy-Actor"

#: Where a canopy deployment serves its Client ID Metadata Document (its
#: ``client_id`` IS this URL) and its client-auth JWKS, relative to its base URL.
CLIENT_METADATA_PATH = "/oauth/client.json"
CLIENT_JWKS_PATH = "/oauth/jwks.json"
#: The arrival endpoint a host POSTs the assertion (and optional ID-JAG) to.
ARRIVAL_PATH = "/api/auth/contact-token"
ARRIVAL_ASSERTION_FIELD = "assertion"
ARRIVAL_ID_JAG_FIELD = "id_jag"
ARRIVAL_AGENT_FIELD = "agent_slug"

AS_METADATA_WELL_KNOWN = "oauth-authorization-server"      # RFC 8414
PRM_WELL_KNOWN = "oauth-protected-resource"                # RFC 9728

#: The live probe (a canopy extension, optional for a host). The RFC 8414
#: metadata field naming the host's probe endpoint — present only while the
#: host has a probe identity configured.
PROBE_ENDPOINT_METADATA_FIELD = "canopy_probe_endpoint"
#: The claim a probe ID-JAG carries (``true``), so a host's audit can tell probe
#: traffic from a real visitor's. Never on an ID-JAG issued for a visitor.
PROBE_CLAIM = "canopy_probe"
#: Form fields that would name WHO or WHAT a probe is for. The probe identity is
#: the host's to fix, so a request carrying any of these is refused rather than
#: silently ignored — a caller that thinks it chose is a caller that is wrong.
PROBE_FORBIDDEN_FIELDS = frozenset({
    "sub", "subject", "login_hint", "user", "user_id", "username", "requested_subject",
    "assertion", "id_jag", "tool", "arguments",
})

#: RFC 6749 / RFC 9449 error codes a host's token endpoint answers with.
OAUTH_ERRORS = frozenset({
    "invalid_request", "invalid_client", "invalid_grant", "invalid_scope",
    "invalid_target", "unsupported_grant_type", "invalid_dpop_proof",
    "use_dpop_nonce",
})

#: The fields of canopy's Client ID Metadata Document, exactly.
CLIENT_METADATA_FIELDS: tuple[str, ...] = (
    "client_id", "client_name", "jwks_uri", "token_endpoint_auth_method",
    "grant_types", "dpop_bound_access_tokens")


class ContractError(Exception):
    """A statement that breaks the contract. ``code`` is stable and safe to log;
    the message names the failed check and never contains a credential."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- encoding helpers ---------------------------------------------------------------------------


def b64url(data: bytes) -> str:
    """Unpadded base64url (RFC 7515 §2)."""
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def ath(access_token: str) -> str:
    """The DPoP ``ath`` claim: base64url(SHA-256(ASCII access token)), RFC 9449 §4.2."""
    return b64url(hashlib.sha256(access_token.encode("ascii")).digest())


def token_checksum(raw: str) -> str:
    """What a host STORES for an access token it issued: hex SHA-256. The raw
    token is never stored."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def constant_time_equal(a, b) -> bool:
    """Constant-time string equality. Anything that is not a string is unequal,
    so a claim of the wrong JSON type can never match."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a.encode(), b.encode())


def jwk_thumbprint(jwk: Mapping) -> str:
    """RFC 7638 SHA-256 thumbprint of a public OKP or EC JWK.

    It is the ``kid`` canopy (and a host) publish, and the ``jkt`` a host binds a
    DPoP token to (``cnf.jkt``), so it must be the value any other library
    computes: the REQUIRED members only, sorted, no whitespace.
    """
    kty = jwk.get("kty") if isinstance(jwk, Mapping) else None
    if kty == "OKP":
        members = ("crv", "kty", "x")
    elif kty == "EC":
        members = ("crv", "kty", "x", "y")
    else:
        raise ContractError("bad_key", f"unsupported key type {kty!r}")
    if not all(isinstance(jwk.get(m), str) for m in members):
        raise ContractError("bad_key", "the key is missing a required member")
    canonical = json.dumps({m: jwk[m] for m in members}, separators=(",", ":"), sort_keys=True)
    return b64url(hashlib.sha256(canonical.encode()).digest())


def normalize_url(url: str) -> str:
    """Trailing slash trimmed, surrounding space dropped — how issuers and
    resources are compared."""
    return (url or "").strip().rstrip("/")


def normalize_htu(url: str) -> str:
    """RFC 9449 §4.3: scheme and host lower-cased, no query or fragment, no
    trailing slash — how a proof's ``htu`` is compared to the request URL."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def metadata_url(issuer: str) -> str:
    """RFC 8414 §3.1: the well-known segment goes between host and path."""
    parts = urlsplit(normalize_url(issuer))
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{parts.netloc}/.well-known/{AS_METADATA_WELL_KNOWN}{path}"


def protected_resource_metadata_url(resource: str) -> str:
    """RFC 9728 §3.1: the same insertion rule, for the resource's metadata."""
    parts = urlsplit(normalize_url(resource))
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{parts.netloc}/.well-known/{PRM_WELL_KNOWN}{path}"


def client_id_for(canopy_base_url: str) -> str:
    """canopy's OAuth ``client_id`` for a deployment: the URL of its CIMD."""
    return normalize_url(canopy_base_url) + CLIENT_METADATA_PATH


def client_metadata_document(client_id: str, jwks_uri: str, *, client_name: str = "canopy") -> dict:
    """The Client ID Metadata Document, exactly the contract's shape."""
    return {
        "client_id": client_id,
        "client_name": client_name,
        "jwks_uri": jwks_uri,
        "token_endpoint_auth_method": TOKEN_ENDPOINT_AUTH_METHOD,
        "grant_types": [JWT_BEARER_GRANT],
        "dpop_bound_access_tokens": True,
    }


def scopes_of(value) -> list[str]:
    """A space-separated scope string as a list, order kept, duplicates dropped."""
    out: list[str] = []
    for scope in str(value or "").split():
        if scope not in out:
            out.append(scope)
    return out


# --- claim validators ------------------------------------------------------------------------


def lifetime(claims: Mapping) -> int:
    return int(claims["exp"]) - int(claims["iat"])


def check_lifetime(claims: Mapping, max_lifetime: int, *, leeway: int = 0,
                   code: str = "too_long", what: str = "a token") -> None:
    """``exp - iat`` must not exceed the cap. ``exp`` alone only proves the
    issuer chose an end; a year-long assertion rebuilds a standing secret."""
    span = lifetime(claims)
    if span > max_lifetime + leeway:
        raise ContractError(code, f"{what} may live at most {max_lifetime}s; this one lives {span}s")


def check_jti(claims: Mapping) -> str:
    """A usable ``jti``: a non-empty string of bounded length."""
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti or len(jti) > MAX_JTI_LENGTH:
        raise ContractError("missing_claim", "the token needs a jti")
    return jti


def check_dpop_iat(iat, *, now: float | None = None) -> int:
    current = time.time() if now is None else now
    if not isinstance(iat, (int, float)) or isinstance(iat, bool) \
            or abs(current - iat) > DPOP_IAT_WINDOW_SECONDS:
        raise ContractError("bad_proof", "the proof is not fresh")
    return int(iat)


def tools_for_scopes(scopes: Iterable[str], scope_tools: Mapping[str, Iterable[str]]) -> frozenset[str]:
    """The union of the tools each scope unlocks. An unknown scope unlocks nothing."""
    allowed: set[str] = set()
    for scope in scopes or ():
        allowed |= set(scope_tools.get(scope, ()))
    return frozenset(allowed)
