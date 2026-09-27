"""JOSE building blocks shared by both sides: reading a header without trusting
it, verifying with OUR algorithm list, and making / checking DPoP proofs.

Two rules run through all of it:

* **The algorithm is chosen by the verifier, never read from the token.**
  ``alg: none`` and HMAC-with-the-public-key-as-secret are the two classic
  forgeries, and both work only against a verifier that lets the attacker pick.
* **Fail closed.** Every function returns a verdict or raises
  ``ContractError``; there is no path that answers "probably fine".
"""
from __future__ import annotations

import time
import uuid

from . import contract
from .contract import ContractError
from .keys import alg_for, header_jwk, public_key_for


def unverified_header(token, *, algorithms=contract.GRANT_ALGORITHMS) -> dict:
    """The JOSE header, size-checked, with ``alg`` inside ``algorithms``. Selects
    a key; decides nothing."""
    import jwt

    if not isinstance(token, str) or not token or len(token) > contract.MAX_JWT_BYTES:
        raise ContractError("malformed", "not a compact JWS of acceptable size")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise ContractError("malformed", "not a compact JWS") from exc
    if header.get("alg") not in algorithms:
        raise ContractError("bad_alg", f"algorithm {header.get('alg')!r} is not accepted")
    return header


def unverified_claims(token) -> dict:
    """Claims read WITHOUT verification — only ever to pick which key to verify
    with (``iss``). Nothing else may be read before the signature is checked."""
    import jwt

    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except Exception as exc:  # noqa: BLE001
        raise ContractError("malformed", f"not a readable token: {exc}") from exc
    if not isinstance(claims, dict):
        raise ContractError("malformed", "not a readable token")
    return claims


def decode(token: str, key, *, algorithms, audience=None, required=(), leeway: int = 0) -> dict:
    """Verify signature and the standard claims. Raises ``ContractError``."""
    import jwt

    options = {"require": list(required), "verify_aud": audience is not None}
    try:
        return jwt.decode(token, key, algorithms=list(algorithms), audience=audience,
                          options=options, leeway=leeway)
    except jwt.ExpiredSignatureError as exc:
        raise ContractError("expired", "the token has expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise ContractError("bad_audience", "the token is for a different audience") from exc
    except jwt.ImmatureSignatureError as exc:
        raise ContractError("not_yet_valid", "the token is not valid yet") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise ContractError("missing_claim", f"the token is missing {exc.claim!r}") from exc
    except jwt.InvalidSignatureError as exc:
        raise ContractError("bad_signature", "the signature does not verify") from exc
    except jwt.PyJWTError as exc:
        raise ContractError("invalid", "the token is not valid") from exc


def sign(claims: dict, key, *, headers: dict | None = None) -> str:
    """Sign with the algorithm the key's type dictates (EdDSA or ES256)."""
    import jwt

    return jwt.encode(claims, key, algorithm=alg_for(key), headers=headers or None)


def new_jti() -> str:
    return str(uuid.uuid4())


# --- DPoP (RFC 9449) ----------------------------------------------------------------


def make_dpop_proof(key, htm: str, htu: str, *, access_token: str | None = None,
                    nonce: str | None = None, now: float | None = None) -> str:
    """A fresh proof for ONE request.

    ``htu`` is the target URI without query or fragment. ``ath`` binds the proof
    to the access token it accompanies, so a proof lifted from one call cannot
    carry a different token. At the token endpoint there is no token yet, and so
    no ``ath``.
    """
    claims: dict = {
        "jti": new_jti(),
        "htm": htm.upper(),
        "htu": htu,
        "iat": int(time.time() if now is None else now),
    }
    if access_token is not None:
        claims["ath"] = contract.ath(access_token)
    if nonce:
        claims["nonce"] = nonce
    return sign(claims, key, headers={"typ": contract.DPOP_TYP, "jwk": header_jwk(key)})


def verify_dpop_proof(proof, *, htm: str, htu: str, access_token: str | None = None,
                      now: float | None = None) -> tuple[str, str, int]:
    """Verify a DPoP proof. Returns ``(jkt, jti, iat)``; the CALLER consumes the jti.

    Checks ``typ``, an allowed ``alg``, an embedded PUBLIC key of the matching
    shape, the signature against that key, ``htm``, ``htu``, ``iat`` within
    ±60s, a ``jti``, and — when an access token is presented — ``ath``. At the
    token endpoint (no access token) a proof carrying ``ath`` is refused.
    """
    header = unverified_header(proof)
    if header.get("typ") != contract.DPOP_TYP:
        raise ContractError("bad_proof", "a DPoP proof must have typ dpop+jwt")
    alg = header["alg"]
    jwk = header.get("jwk")
    key = public_key_for(jwk, alg)
    # The leeway lets PyJWT's own "iat is not in the future" check absorb the
    # same skew the window below allows; the window is the real freshness rule.
    claims = decode(proof, key, algorithms=[alg], required=contract.DPOP_REQUIRED_CLAIMS,
                    leeway=contract.DPOP_IAT_WINDOW_SECONDS)

    if not contract.constant_time_equal(claims.get("htm"), htm):
        raise ContractError("bad_proof", "the proof is for a different HTTP method")
    if not contract.constant_time_equal(contract.normalize_htu(claims.get("htu") or ""),
                                        contract.normalize_htu(htu)):
        raise ContractError("bad_proof", "the proof is for a different URL")
    iat = contract.check_dpop_iat(claims.get("iat"), now=now)

    if access_token is not None:
        try:
            expected = contract.ath(access_token)
        except UnicodeEncodeError:
            raise ContractError("bad_proof", "the access token is not ASCII") from None
        if not contract.constant_time_equal(claims.get("ath"), expected):
            raise ContractError("bad_proof", "the proof is not bound to this access token")
    elif "ath" in claims:
        raise ContractError("bad_proof", "a proof at the token endpoint carries no ath")

    jti = contract.check_jti(claims)
    return contract.jwk_thumbprint(jwk), jti, iat
