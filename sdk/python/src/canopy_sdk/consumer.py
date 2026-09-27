"""canopy's side of the contract: verifying what a host signs, and signing what
canopy sends when it redeems a grant and calls a host's MCP.

canopy-web imports these (``apps/tokens/assertions.py``, ``host_grants.py``,
``client_identity.py``, ``host_gateway.py``) instead of keeping its own copies,
and its CI runs them against ``canopy_sdk.host`` in a round trip — which is what
stops the two halves drifting.

What stays in canopy-web: WHICH keys a site has registered (a database row plus
a fetched JWKS), where used ``jti``s live (its cache), where a redeemed token is
stored (encrypted), and every outbound request's transport. Each is passed in
here as a value or a callable, so nothing in this module reads settings or does
I/O unless handed a function that does.

Refusal codes are stable API: canopy-web records them in its audit and event
logs, and returns ``<code>: <message>`` to a host.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from . import contract
from .contract import constant_time_equal, normalize_url
from .jose import make_dpop_proof, new_jti, sign
from .keys import alg_for, load_private_key, public_jwk


class ConsumerError(Exception):
    """A refusal with a stable ``code``. The message never contains a credential."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AssertionRefused(ConsumerError):
    """The visitor assertion was refused."""


class RedemptionRefused(ConsumerError):
    """The host grant (ID-JAG) or its redemption was refused."""


# --- the visitor assertion ----------------------------------------------------------


def unverified_issuer(token: str) -> str:
    """``iss`` read WITHOUT trusting it — it only selects which site's keys to
    verify against. Nothing else is read before the signature is checked."""
    import jwt

    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except Exception as exc:  # noqa: BLE001
        raise AssertionRefused("malformed", f"not a readable assertion: {exc}") from exc
    iss = (claims.get("iss") or "").strip()
    if not iss:
        raise AssertionRefused("no_issuer", "the assertion does not say which app issued it")
    return iss


def verify_visitor_assertion(token: str, keys: Sequence, *, audience: str, label: str = "the site",
                             spend_jti: Callable[[dict], None] | None = None,
                             algorithms: Sequence[str] = contract.ASSERTION_ALGORITHMS,
                             leeway: int = contract.LEEWAY_SECONDS,
                             max_lifetime: int = contract.ASSERTION_MAX_LIFETIME) -> dict:
    """Check a host's visitor assertion against its keys. Returns its claims.

    Strict, because every JWT vulnerability worth the name comes from leniency:
    OUR algorithm list (never the header's), ``aud`` must match, ``exp`` required
    AND capped, and ``spend_jti(claims)`` — the caller's single-use store — runs
    before the claims are returned. Raises ``AssertionRefused`` for every
    refusal and never returns a partial result.
    """
    import jwt

    claims = None
    last_error: Exception | None = None
    for key in keys:
        try:
            claims = jwt.decode(
                token, key,
                # OURS, not the token's. This one argument is the difference
                # between a verifier and a forgery oracle.
                algorithms=list(algorithms),
                audience=audience,
                leeway=leeway,
                options={"require": list(contract.ASSERTION_REQUIRED_CLAIMS),
                         "verify_exp": True, "verify_aud": True, "verify_iat": True},
            )
            break
        except jwt.InvalidAudienceError as exc:
            raise AssertionRefused(
                "wrong_audience", f"the assertion is addressed elsewhere; this canopy is {audience!r}") from exc
        except jwt.ExpiredSignatureError as exc:
            raise AssertionRefused("expired", "the assertion has expired") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise AssertionRefused("incomplete", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - try the next key
            last_error = exc
    if claims is None:
        # Every registered key rejected it. Rotation is why there is more than
        # one; exhausting them means this was not signed by this site.
        raise AssertionRefused(
            "bad_signature", f"no registered key for {label!r} verifies this assertion ({last_error})")

    span = contract.lifetime(claims)
    if span > max_lifetime + leeway:
        raise AssertionRefused(
            "too_long", f"assertions may live at most {max_lifetime}s; this one lives {span}s")

    if spend_jti is not None:
        spend_jti(claims)

    if not str(claims.get("sub") or "").strip():
        raise AssertionRefused("no_subject", "the assertion does not say who it is about")
    return claims


# --- the ID-JAG ------------------------------------------------------------------------


def check_id_jag(id_jag: str, keys, *, issuer: str, client_id: str, resource: str, subject: str,
                 label: str = "the site", algorithms: Sequence[str] = contract.GRANT_ALGORITHMS,
                 leeway: int = contract.LEEWAY_SECONDS,
                 max_lifetime: int = contract.ID_JAG_MAX_LIFETIME) -> dict:
    """Verify a host's grant BEFORE canopy spends it. Returns its claims.

    ``keys`` is a sequence, or a zero-argument callable returning one — called
    only after the header has been checked, so resolving a site's keys (which may
    fetch) is not spent on something that is not an ID-JAG. The host checks all
    of this again; canopy checking first means a grant naming somebody else, or
    another resource, is never even sent.
    """
    import jwt

    try:
        header = jwt.get_unverified_header(id_jag)
    except Exception as exc:  # noqa: BLE001
        raise RedemptionRefused("malformed", "the id_jag is not a readable JWT") from exc
    if header.get("typ") != contract.ID_JAG_TYP:
        raise RedemptionRefused("wrong_type", f"an id_jag must have typ {contract.ID_JAG_TYP!r}")
    if callable(keys):
        keys = keys()

    issuer = normalize_url(issuer)
    claims = None
    last: Exception | None = None
    for key in keys:
        try:
            claims = jwt.decode(
                id_jag, key,
                algorithms=list(algorithms),      # ours, never the header's
                audience=[issuer, issuer + "/"],
                leeway=leeway,
                options={"require": list(contract.ID_JAG_REQUIRED_CLAIMS)},
            )
            break
        except jwt.InvalidAudienceError as exc:
            raise RedemptionRefused("wrong_audience", "the id_jag is not addressed to the "
                                    "site's own authorization server") from exc
        except jwt.ExpiredSignatureError as exc:
            raise RedemptionRefused("expired", "the id_jag has expired") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise RedemptionRefused("incomplete", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - try the next key
            last = exc
    if claims is None:
        raise RedemptionRefused("bad_signature",
                                f"no key registered for {label!r} verifies the id_jag "
                                f"({type(last).__name__ if last else 'no key'})")

    if not constant_time_equal(normalize_url(str(claims["iss"])), issuer):
        raise RedemptionRefused("wrong_issuer", "the id_jag was not issued by the site's issuer")
    if not constant_time_equal(str(claims["client_id"]), client_id):
        raise RedemptionRefused("wrong_client", "the id_jag is for a different client")
    if not constant_time_equal(normalize_url(str(claims["resource"])), normalize_url(resource)):
        raise RedemptionRefused("wrong_resource",
                                "the id_jag names a resource other than the site's MCP server")
    if not constant_time_equal(str(claims["sub"]), subject):
        raise RedemptionRefused("wrong_subject",
                                "the id_jag names a different person than the arrival assertion")
    if contract.lifetime(claims) > max_lifetime + leeway:
        raise RedemptionRefused("too_long", f"an id_jag may live at most {max_lifetime}s")
    return claims


# --- what canopy signs ---------------------------------------------------------------------


def client_assertion(key, client_id: str, audience: str, *, now: float | None = None) -> str:
    """RFC 7523 ``private_key_jwt``: iss = sub = client_id, aud = the host's
    issuer, exp ≤ 60s, single-use jti, ``kid`` = the key's thumbprint."""
    issued = int(time.time() if now is None else now)
    claims = {
        "iss": client_id,
        "sub": client_id,
        "aud": audience,
        "iat": issued,
        "exp": issued + contract.CLIENT_ASSERTION_MAX_LIFETIME,
        "jti": new_jti(),
    }
    return sign(claims, key, headers={"kid": public_jwk(key)["kid"]})


def dpop_proof(key, htm: str, htu: str, *, access_token: str | None = None,
               nonce: str | None = None) -> str:
    """A fresh RFC 9449 proof for ONE request, signed by canopy's DPoP key."""
    return make_dpop_proof(key, htm, htu, access_token=access_token, nonce=nonce)


def dpop_jkt(key) -> str:
    """The thumbprint a DPoP-bound token is bound to (``cnf.jkt``)."""
    return public_jwk(key)["kid"]


def dpop_authorization(access_token: str) -> str:
    return f"DPoP {access_token}"


@dataclass
class ClientCredentials:
    """canopy as an OAuth client of a host: its ``client_id`` (a CIMD URL), the
    key that authenticates it, and the DIFFERENT key its tokens are bound to.
    A leak of one does not do the other's job, and the DPoP key is never
    published — a host learns it from each proof's ``jwk`` header."""

    client_id: str
    client_key: object
    dpop_key: object

    def __post_init__(self):
        self.client_key = load_private_key(self.client_key, what="the client key")
        self.dpop_key = load_private_key(self.dpop_key, what="the DPoP key")

    @classmethod
    def generate(cls, client_id: str, alg: str = "EdDSA") -> ClientCredentials:
        from .keys import generate_private_key

        return cls(client_id, generate_private_key(alg), generate_private_key(alg))

    def client_assertion(self, audience: str) -> str:
        return client_assertion(self.client_key, self.client_id, audience)

    def dpop_proof(self, htm: str, htu: str, *, access_token: str | None = None,
                   nonce: str | None = None) -> str:
        return dpop_proof(self.dpop_key, htm, htu, access_token=access_token, nonce=nonce)

    @property
    def dpop_jkt(self) -> str:
        return dpop_jkt(self.dpop_key)

    def jwks(self) -> dict:
        return {"keys": [{**public_jwk(self.client_key), "use": "sig"}]}

    def metadata(self, jwks_uri: str) -> dict:
        return contract.client_metadata_document(self.client_id, jwks_uri)

    @property
    def alg(self) -> str:
        return alg_for(self.client_key)


# --- redemption --------------------------------------------------------------------------


def validate_authorization_server_metadata(doc: Mapping, issuer: str) -> str:
    """The token endpoint from a host's RFC 8414 document — refusing unless the
    document names the configured issuer. Returns the endpoint (the caller still
    vets it as an outbound URL)."""
    issuer = normalize_url(issuer)
    if not constant_time_equal(normalize_url(str(doc.get("issuer") or "")), issuer):
        raise RedemptionRefused("issuer_mismatch",
                                f"the metadata at {contract.metadata_url(issuer)} names a different issuer")
    return str(doc.get("token_endpoint") or "")


def redemption_form(id_jag: str, *, client_id: str, resource: str) -> dict:
    """The RFC 7523 form, without the client assertion (it is minted per attempt)."""
    return {
        "grant_type": contract.JWT_BEARER_GRANT,
        "assertion": id_jag,
        "client_id": client_id,
        "client_assertion_type": contract.CLIENT_ASSERTION_TYPE,
        "resource": resource,
    }


def request_token(post_form: Callable, endpoint: str, form: Mapping, *, audience: str,
                  client_assertion: Callable[[str], str], dpop_proof: Callable[..., str]):
    """One POST, retried ONCE if the host demands a DPoP nonce (RFC 9449 §8).

    A fresh client assertion AND proof per attempt: both are single use.
    ``post_form(url, data, headers=...)`` returns ``(status, json, headers)``.
    Returns ``(status, json)``.
    """
    nonce = None
    status, doc = 0, {}
    for _ in range(2):
        body = {**form, "client_assertion": client_assertion(audience)}
        proof = dpop_proof("POST", endpoint, nonce=nonce)
        status, doc, headers = post_form(endpoint, body, headers={contract.DPOP_HEADER: proof},
                                         what="token endpoint")
        if status == 400 and doc.get("error") == "use_dpop_nonce" \
                and headers.get(contract.DPOP_NONCE_HEADER) and nonce is None:
            nonce = headers[contract.DPOP_NONCE_HEADER]
            continue
        return status, doc
    return status, doc


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    #: Clamped to the contract's cap — a host bug must not make a visitor's
    #: token outlive the visit.
    expires_in: int
    scope: str

    def __repr__(self) -> str:  # never print the token
        return f"TokenResponse(expires_in={self.expires_in}, scope={self.scope!r})"


def parse_token_response(status: int, doc: Mapping, *,
                         max_lifetime: int = contract.ACCESS_TOKEN_MAX_LIFETIME) -> TokenResponse:
    """The host's answer, checked. Only the OAuth error CODE of a refusal is
    kept — ``error_description`` is the host's prose and could echo anything."""
    if status != 200:
        raise RedemptionRefused("redeem_refused",
                                f"the host refused the grant ({status} {doc.get('error') or 'error'})")
    token = doc.get("access_token")
    if not isinstance(token, str) or not token:
        raise RedemptionRefused("bad_response", "the host returned no access_token")
    if str(doc.get("token_type") or "").lower() != contract.TOKEN_TYPE_DPOP.lower():
        # A bearer token here would be usable by anyone who copied it; the
        # contract says DPoP, so anything else is refused rather than stored.
        raise RedemptionRefused("not_dpop", "the host returned a token that is not DPoP-bound")
    try:
        lifetime = int(doc.get("expires_in") or 0)
    except (TypeError, ValueError):
        lifetime = 0
    if lifetime <= 0:
        raise RedemptionRefused("bad_response", "the host returned no expires_in")
    return TokenResponse(access_token=token, expires_in=min(lifetime, max_lifetime),
                         scope=str(doc.get("scope") or ""))


def redeem_id_jag(id_jag: str, *, issuer: str, resource: str, credentials: ClientCredentials,
                  fetch_json: Callable[[str], dict] | None = None,
                  post_form: Callable | None = None) -> TokenResponse:
    """The whole redemption for a consumer with no storage of its own (a
    conformance run, a script): discover → validate issuer → POST → parse.
    canopy-web composes the same pieces around its cache and outbound guard."""
    from . import fetch

    get = fetch_json or fetch.get_json
    post = post_form or (lambda url, data, headers, what="": fetch.post_form(url, data, headers=headers))
    try:
        doc = get(contract.metadata_url(issuer))
    except fetch.FetchError as exc:
        raise RedemptionRefused("metadata_unreachable", str(exc)) from exc
    endpoint = validate_authorization_server_metadata(doc, issuer)
    try:
        fetch.vet_url(endpoint)
    except fetch.FetchError as exc:
        raise RedemptionRefused("bad_token_endpoint", str(exc)) from exc
    try:
        status, body = request_token(
            post, endpoint, redemption_form(id_jag, client_id=credentials.client_id, resource=resource),
            audience=normalize_url(issuer), client_assertion=credentials.client_assertion,
            dpop_proof=credentials.dpop_proof)
    except fetch.FetchError as exc:
        raise RedemptionRefused("token_endpoint_unreachable", str(exc)) from exc
    return parse_token_response(status, body)
