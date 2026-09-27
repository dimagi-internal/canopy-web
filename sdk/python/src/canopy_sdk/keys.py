"""Keys: loading them, describing them as JWKs, and turning a JWK back into a
verification key — asymmetric only, with the key's shape checked against the
algorithm.

Every signature check in this package is PyJWT's (with ``cryptography``
underneath); this module only decides WHICH key a check may use.
"""
from __future__ import annotations

from collections.abc import Mapping

from .contract import GRANT_ALGORITHMS, KEY_SHAPES, PRIVATE_JWK_MEMBERS, ContractError, jwk_thumbprint


class KeyError_(ContractError):
    """A key that cannot be used for this contract. Subclass of ContractError."""


def load_private_key(pem, *, what: str = "the key"):
    """A PEM (str or bytes) or an already-loaded key → an Ed25519 or P-256
    private key. Anything symmetric or RSA is refused: every key a party SIGNS
    with in this contract is EdDSA or ES256."""
    from cryptography.hazmat.primitives import serialization

    key = pem
    if isinstance(pem, (str, bytes)):
        raw = pem.encode() if isinstance(pem, str) else pem
        try:
            key = serialization.load_pem_private_key(raw, password=None)
        except Exception as exc:  # noqa: BLE001 - any parse failure is a refusal
            raise KeyError_("bad_key", f"{what} is not a readable PEM private key") from exc
    check_signing_key(key, what=what)
    return key


def check_signing_key(key, *, what: str = "the key") -> None:
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519

    if isinstance(key, ed25519.Ed25519PrivateKey):
        return
    if isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name == "secp256r1":
        return
    raise KeyError_(
        "bad_key",
        f"{what} must be an Ed25519 or P-256 private key — nothing symmetric, "
        "nothing RSA: the contract allows EdDSA and ES256 only",
    )


def generate_private_key(alg: str = "EdDSA"):
    """A fresh key for tests, dev, and conformance runs."""
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519

    if alg == "EdDSA":
        return ed25519.Ed25519PrivateKey.generate()
    if alg == "ES256":
        return ec.generate_private_key(ec.SECP256R1())
    raise KeyError_("bad_alg", f"algorithm {alg!r} is not one the contract signs with")


def private_pem(key) -> str:
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def public_pem(key) -> str:
    from cryptography.hazmat.primitives import serialization

    pub = key.public_key() if hasattr(key, "public_key") else key
    return pub.public_bytes(serialization.Encoding.PEM,
                            serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def alg_for(key) -> str:
    """The contract algorithm a key signs with, from its type."""
    from cryptography.hazmat.primitives.asymmetric import ed25519

    if isinstance(key, (ed25519.Ed25519PrivateKey, ed25519.Ed25519PublicKey)):
        return "EdDSA"
    return "ES256"


def public_jwk(key) -> dict:
    """The public half as a JWK, ``kid`` = its RFC 7638 thumbprint, ``alg`` set.

    Only public members: this is published, and a ``d`` member would publish the
    ability to sign. Accepts a private or a public key.
    """
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from jwt.algorithms import ECAlgorithm, OKPAlgorithm

    pub = key.public_key() if hasattr(key, "private_bytes") else key
    if isinstance(pub, ed25519.Ed25519PublicKey):
        jwk = OKPAlgorithm.to_jwk(pub, as_dict=True)
    else:
        jwk = ECAlgorithm.to_jwk(pub, as_dict=True)
    jwk = {k: v for k, v in jwk.items() if k not in PRIVATE_JWK_MEMBERS}
    jwk["kid"] = jwk_thumbprint(jwk)
    jwk["alg"] = alg_for(pub)
    return jwk


def header_jwk(key) -> dict:
    """The JWK a DPoP proof carries in its header: public members only, no
    ``kid``/``alg`` (the verifier derives both)."""
    return {k: v for k, v in public_jwk(key).items() if k not in ("kid", "alg")}


def public_key_for(jwk, alg: str, *, algorithms=GRANT_ALGORITHMS):
    """The verification key a JWK names — if it is a PUBLIC key of exactly the
    shape ``alg`` needs. Raises ``ContractError('bad_alg'|'bad_key')``."""
    from jwt import PyJWK

    if alg not in algorithms or alg not in KEY_SHAPES:
        raise ContractError("bad_alg", f"algorithm {alg!r} is not accepted")
    if not isinstance(jwk, Mapping):
        raise ContractError("bad_key", "the key is not a JWK object")
    if PRIVATE_JWK_MEMBERS & set(jwk):
        raise ContractError("bad_key", "a public key must not carry private members")
    kty, crv = KEY_SHAPES[alg]
    if jwk.get("kty") != kty or jwk.get("crv") != crv:
        raise ContractError("bad_key", f"{alg} needs a {kty}/{crv} key")
    if jwk.get("alg") not in (None, alg):
        raise ContractError("bad_key", "the key is declared for a different algorithm")
    try:
        return PyJWK.from_dict(dict(jwk), algorithm=alg).key
    except Exception as exc:  # noqa: BLE001 -- any parse failure is a refusal
        raise ContractError("bad_key", "the key could not be read") from exc


def select_key(keys, kid) -> dict | None:
    """The one signing JWK in ``keys`` named by ``kid``. Without a ``kid`` the
    choice is unambiguous only when there is exactly one signing key."""
    from .contract import constant_time_equal

    signing = [k for k in (keys or []) if isinstance(k, Mapping) and k.get("use") in (None, "sig")]
    if kid is None:
        return dict(signing[0]) if len(signing) == 1 else None
    matches = [k for k in signing if constant_time_equal(k.get("kid"), kid)]
    return dict(matches[0]) if len(matches) == 1 else None
