"""canopy-web's own key as a sealed-connection recipient.

A connection's envelopes (`zks1:`) may name canopy-web as one more `device` recipient. Its P-256
key lives in KMS (deploy/aws/canopy-web-connections-key.cfn.yaml) and is used only through
`DeriveSharedSecret`: the private key never leaves KMS, and canopy-web never holds a derived
secret longer than one open. `CONNECTIONS_SERVER_KMS_KEY` (a key id, ARN or alias) names it;
unset, `available()` is false and every connection behaves as switched off.

Nothing here logs or raises with a secret: a KMS failure is `Unavailable` with a fixed message.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings

from apps.agents.credentials import zk

KEY_SPEC, KEY_USAGE = "ECC_NIST_P256", "KEY_AGREEMENT"


class Unavailable(Exception):  # noqa: N818 — the name is the interface
    """KMS can't be used right now (unset, denied, down, wrong key). The message is fixed."""


_lock = threading.Lock()
_cached: tuple[str, dict] | None = None  # (key id, public JWK)
_software: ec.EllipticCurvePrivateKey | None = None


def _reset() -> None:
    global _cached
    with _lock:
        _cached = None


def use_software_key_for_tests(private_key) -> None:
    """Stand in a local P-256 key for KMS (None puts KMS back). Tests only."""
    global _software
    if not getattr(settings, "TESTING", False):
        raise RuntimeError("use_software_key_for_tests is for tests only")
    _software = private_key
    _reset()


def available() -> bool:
    return _software is not None or bool(getattr(settings, "CONNECTIONS_SERVER_KMS_KEY", ""))


def _client():
    # Short timeouts: a sign-in must not hang on KMS being down.
    cfg = Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 1, "mode": "standard"})
    return boto3.client("kms", region_name=getattr(settings, "AWS_REGION", None) or None, config=cfg)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _jwk(pub: ec.EllipticCurvePublicKey) -> dict:
    p = pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"kty": "EC", "crv": "P-256", "x": _b64u(p[1:33]), "y": _b64u(p[33:])}


def public_jwk() -> dict:
    """The key's public JWK (cached per process)."""
    global _cached
    if not available():
        raise Unavailable("canopy-web's connection key is not configured")
    if _software is not None:
        return _jwk(_software.public_key())
    key_id = settings.CONNECTIONS_SERVER_KMS_KEY
    with _lock:
        hit = _cached
    if hit is not None and hit[0] == key_id:
        return dict(hit[1])
    try:  # the network call holds no lock
        out = _client().get_public_key(KeyId=key_id)
        if out.get("KeySpec") != KEY_SPEC or out.get("KeyUsage") != KEY_USAGE:
            raise Unavailable("canopy-web's connection key is not a P-256 key-agreement key")
        jwk = _jwk(serialization.load_der_public_key(bytes(out["PublicKey"])))
    except Unavailable:
        raise
    except (ClientError, BotoCoreError, KeyError, ValueError, TypeError):
        raise Unavailable("canopy-web's connection key could not be read") from None
    with _lock:
        _cached = (key_id, jwk)
    return dict(jwk)


def thumbprint() -> str:
    """RFC 7638 thumbprint of the public key — what a statement's `canopy_web` names."""
    j = public_jwk()
    canon = json.dumps({"crv": j["crv"], "kty": j["kty"], "x": j["x"], "y": j["y"]},
                       separators=(",", ":"), sort_keys=True)
    return _b64u(hashlib.sha256(canon.encode()).digest())


def derive(peer_jwk: dict) -> bytes:
    """The raw ECDH secret between this key and `peer_jwk`. Never log it."""
    if not available():
        raise Unavailable("canopy-web's connection key is not configured")
    try:
        peer = zk.public_key(peer_jwk)
    except (zk.ZkError, ValueError, TypeError):
        raise Unavailable("the sealed value's key is not a P-256 key") from None
    if _software is not None:
        return _software.exchange(ec.ECDH(), peer)
    der = peer.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    try:
        out = _client().derive_shared_secret(KeyId=settings.CONNECTIONS_SERVER_KMS_KEY,
                                             KeyAgreementAlgorithm="ECDH", PublicKey=der)
        return bytes(out["SharedSecret"])
    except (ClientError, BotoCoreError, KeyError, TypeError):
        raise Unavailable("canopy-web's connection key could not derive") from None
