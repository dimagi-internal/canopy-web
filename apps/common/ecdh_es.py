"""ECDH-ES on P-256: the vault's wrap (`frontend/src/vault/zk.ts::wrapEcdhEs`), for the
server's side of a WRITE-ONLY path.

canopy-web uses it to seal something to a public key whose private half it does not
hold: a sensitive conversation's words to the conversation's key, and that key's
private half to each of its people's vault keys (`canopy_sessions.conversation_keys`).
It has no unwrap on purpose: nothing here can open what it makes.

The construction, byte for byte as `wrapEcdhEs` (docs/vault/zk-v1/zk_v1_reference.py §3):

    E      = a fresh P-256 key pair
    Z      = ECDH(E, Q)                       (the 32-byte x-coordinate)
    KEK    = HKDF-SHA256(Z, salt = E_pub || Q_pub (X9.62 uncompressed), info, L = 32)
    ct     = AES-256-GCM(KEK, nonce12, plaintext, aad = info)   (tag appended)
    wrap   = {"alg": "ecdh-es", "epk": b64u(E_pub), "n": b64u(nonce), "ct": b64u(ct)}

base64url without padding throughout.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ALG = "ecdh-es"
NONCE_LEN = 12
TAG_LEN = 16
POINT_LEN = 65


class ShapeError(ValueError):
    """A wrap or key that is not the shape this construction makes."""


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64u(value: str) -> bytes:
    if not isinstance(value, str) or not all(c.isalnum() or c in "-_" for c in value):
        raise ShapeError("not base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _point(pub: ec.EllipticCurvePublicKey) -> bytes:
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def jwk_of(pub: ec.EllipticCurvePublicKey) -> dict:
    p = _point(pub)
    return {"kty": "EC", "crv": "P-256", "x": b64u(p[1:33]), "y": b64u(p[33:])}


def public_key(jwk: dict) -> ec.EllipticCurvePublicKey:
    """A P-256 public key from its JWK; ShapeError for anything else."""
    if not isinstance(jwk, dict):
        raise ShapeError("a public key is a P-256 JWK")
    x, y = unb64u(jwk.get("x", "")), unb64u(jwk.get("y", ""))
    if len(x) != 32 or len(y) != 32:
        raise ShapeError("not a P-256 public key")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + x + y)
    except ValueError as exc:
        raise ShapeError("not a point on P-256") from exc


def jkt(jwk: dict) -> str:
    """RFC 7638 thumbprint of an EC public JWK (as `zk.ts::jkt` computes it)."""
    canon = json.dumps({"crv": "P-256", "kty": "EC", "x": jwk.get("x", ""), "y": jwk.get("y", "")},
                       separators=(",", ":"), sort_keys=True)
    return b64u(hashlib.sha256(canon.encode()).digest())


def generate() -> tuple[bytes, dict]:
    """A fresh key pair: (its 32-byte private scalar, its public JWK). The caller
    wraps the scalar and lets it go; nothing here keeps it."""
    priv = ec.generate_private_key(ec.SECP256R1())
    return priv.private_numbers().private_value.to_bytes(32, "big"), jwk_of(priv.public_key())


def wrap(plaintext: bytes, recipient_jwk: dict, info: str, *,
         _ephemeral: ec.EllipticCurvePrivateKey | None = None, _nonce: bytes | None = None) -> dict:
    """`plaintext` sealed to `recipient_jwk` under `info`. `_ephemeral` / `_nonce`
    exist only so the cross-language vector can pin the randomness."""
    q = public_key(recipient_jwk)
    e = _ephemeral or ec.generate_private_key(ec.SECP256R1())
    nonce = _nonce or os.urandom(NONCE_LEN)
    epk = _point(e.public_key())
    kek = HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + _point(q),
               info=info.encode()).derive(e.exchange(ec.ECDH(), q))
    return {"alg": ALG, "epk": b64u(epk), "n": b64u(nonce),
            "ct": b64u(AESGCM(kek).encrypt(nonce, plaintext, info.encode()))}


def check_wrap(value, *, plaintext_len: int | None = None) -> dict:
    """Shape only — the server cannot open a wrap, so this is all it can check:
    `{alg: "ecdh-es", epk: a P-256 point, n: 12 bytes, ct: ≥ a tag}`."""
    if not isinstance(value, dict) or set(value) - {"alg", "epk", "n", "ct"} or value.get("alg") != ALG:
        raise ShapeError('a wrap is {"alg": "ecdh-es", "epk", "n", "ct"}')
    epk = unb64u(value.get("epk", ""))
    if len(epk) != POINT_LEN or epk[0] != 4:
        raise ShapeError("epk must be an uncompressed P-256 point")
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), epk)
    except ValueError as exc:
        raise ShapeError("epk is not a point on P-256") from exc
    if len(unb64u(value.get("n", ""))) != NONCE_LEN:
        raise ShapeError("n must be a 12-byte nonce")
    ct = unb64u(value.get("ct", ""))
    if len(ct) < TAG_LEN or (plaintext_len is not None and len(ct) != plaintext_len + TAG_LEN):
        raise ShapeError("ct is not the length this wrap must have")
    return {"alg": ALG, "epk": value["epk"], "n": value["n"], "ct": value["ct"]}
