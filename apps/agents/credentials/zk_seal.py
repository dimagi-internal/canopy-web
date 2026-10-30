"""Server-minted secrets for a zero-knowledge agent — `zks1:` "sealed to
recipients" (canopy-web#1550, §6 of docs/vault/zk-v1/zk_v1_reference.py).

canopy-web itself creates some secrets: the rotated `canopy-pat`. (The Gmail
token from "Connect Google mailbox" is made on a runner and arrives here already
encrypted — `google_mint.py`.) It cannot encrypt them under a ZK agent's
vault key (it never holds one), and storing them custodially would break the
promise. So it seals each value directly to every principal that may read the
agent — each eligible device's ECDH key and each member's vault key — and keeps
no copy of the plaintext. canopy-web saw the value once because it MADE it;
it cannot read the stored result.

The next holder of the vault key to touch the agent (a member's browser, or the
`canopy` CLI on a device) re-encrypts each `zks1` value under the vault key as
an ordinary `zk1` value — "settling" it — so devices approved later can read it.

This is the ONE place canopy-web encrypts for a zero-knowledge agent; `zk.py`
still only shape-checks.
"""
from __future__ import annotations

import json
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from apps.passkeys.models import UserVaultKeyset

from . import vault_members, zk
from .zk import MAX_VALUE_ZK_LEN, NONCE_LEN, TAG_LEN, ZkError, _keys, _shape, b64u_bytes

SEALED_PREFIX = "zks1:"
#: A value is sealed to at most this many principals (devices + members).
MAX_RECIPIENTS = 64


def _b64u(b: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def seal_info(agent_id, name: str, kv: int, kind: str, target: str) -> str:
    return f"canopy-vault/v1/seal-value|{agent_id}|{name}|{kv}|{kind}:{target}"


def value_aad(agent_id, name: str, kv: int) -> bytes:
    return f"canopy-vault/v1/value|{agent_id}|{name}|{kv}".encode()


def _point(pub: ec.EllipticCurvePublicKey) -> bytes:
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def seal_to(plaintext: str, *, agent_id, name: str, kv: int, recipients: list[tuple[str, str, dict]],
            _ephemeral=None, _nonce: bytes | None = None) -> str:
    """`zks1:` value sealed to each `(kind, target, public_jwk)`. `_ephemeral` /
    `_nonce` exist only so the vector test can pin the randomness."""
    if not recipients:
        raise ZkError(409, "zk_no_recipients",
                      "nobody can read this agent's secrets yet: approve a device or set up a vault key")
    if len(recipients) > MAX_RECIPIENTS:
        raise ZkError(422, "zk_too_many_recipients", f"at most {MAX_RECIPIENTS} recipients")
    aad = value_aad(agent_id, name, kv)
    out = []
    for kind, target, jwk in recipients:
        q = zk.public_key(jwk)
        e = _ephemeral or ec.generate_private_key(ec.SECP256R1())
        nonce = _nonce or os.urandom(NONCE_LEN)
        epk = _point(e.public_key())
        info = seal_info(agent_id, name, kv, kind, target)
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"",
                   info=info.encode()).derive(e.exchange(ec.ECDH(), q))
        epk_jwk = {"kty": "EC", "crv": "P-256", "x": _b64u(epk[1:33]), "y": _b64u(epk[33:])}
        out.append({"kind": kind, "target": target, "epk": epk_jwk,
                    "ct": _b64u(nonce + AESGCM(key).encrypt(nonce, plaintext.encode(), aad))})
    body = json.dumps({"kv": kv, "r": out}, separators=(",", ":"))
    return SEALED_PREFIX + _b64u(body.encode())


def recipients_for(agent) -> list[tuple[str, str, dict]]:
    """Every principal that may read `agent` now: each eligible device (target =
    its ECDH key's thumbprint) and each member with a vault key (target = user id)."""

    out = [("device", k.ecdh_jkt, k.ecdh_public_jwk) for k in zk.eligible_device_keys(agent)]
    out += [("member", str(ks.user_id), ks.public_jwk) for ks in zk.eligible_members(agent)]
    return out


def has_recipients(agent, facts=None) -> bool:
    """Would `recipients_for(agent)` name anyone? Asked per agent by the setup checklist
    (and so by the workspace health page, for every agent in a tree), so it never loads
    every vault keyset in the system: one exists() for the devices, then one for the
    keysets of the members of the agent's vault (`vault_members.agent_member_ids`)."""

    if zk.eligible_device_keys_qs(agent, facts).exists():
        return True
    ids = (facts.once(("vault-members", agent.pk), lambda: vault_members.agent_member_ids(agent)) if facts
           else vault_members.agent_member_ids(agent))
    return UserVaultKeyset.objects.filter(user_id__in=ids).exists()


def sealed_targets(value: str) -> set[tuple[str, str]]:
    """The `(kind, target)` pairs a `zks1:` value was sealed to; empty for
    anything else (or a value too malformed to read — it was checked on write)."""
    if not isinstance(value, str) or not value.startswith(SEALED_PREFIX):
        return set()
    try:
        body = json.loads(b64u_bytes(value[len(SEALED_PREFIX):], "value_zk"))
        return {(r["kind"], r["target"]) for r in body["r"]}
    except (ValueError, KeyError, TypeError, ZkError):
        return set()


def keep_only(value: str, allowed: set[tuple[str, str]]) -> tuple[str, set[tuple[str, str]]]:
    """`(value with only the recipients in allowed, the (kind, target) pairs dropped)`.
    Each recipient carries its own ciphertext, so dropping one decrypts nothing; the
    value is returned unchanged when nothing is dropped (or it is not a `zks1:` value)."""
    if not isinstance(value, str) or not value.startswith(SEALED_PREFIX):
        return value, set()
    body = json.loads(b64u_bytes(value[len(SEALED_PREFIX):], "value_zk"))
    dropped = {(r["kind"], r["target"]) for r in body["r"]} - allowed
    if not dropped:
        return value, set()
    body["r"] = [r for r in body["r"] if (r["kind"], r["target"]) in allowed]
    return SEALED_PREFIX + _b64u(json.dumps(body, separators=(",", ":")).encode()), dropped


def seal_for_agent(agent, name: str, plaintext: str) -> str:
    """Seal one server-minted value to everyone who may read `agent`, at its kv."""
    return seal_to(plaintext, agent_id=str(agent.pk), name=name, kv=agent.zk_kv,
                   recipients=recipients_for(agent))


def check_sealed_value(value, name: str = "value") -> str:
    """Shape only: `zks1:` + b64u(JSON {"kv", "r": [{kind, target, epk (P-256 JWK),
    ct (b64u nonce12 || ciphertext+tag)}]})."""

    if not isinstance(value, str) or not value.startswith(SEALED_PREFIX):
        raise _shape(f"{name}: a sealed value must start with {SEALED_PREFIX!r}")
    if len(value) > MAX_VALUE_ZK_LEN:
        raise _shape(f"{name}: value_zk is too long")
    try:
        body = json.loads(b64u_bytes(value[len(SEALED_PREFIX):], f"{name}: value_zk"))
    except ValueError as exc:
        raise _shape(f"{name}: a sealed value's body must be JSON") from exc
    _keys(body, {"kv", "r"}, f"{name}: sealed value")
    if type(body["kv"]) is not int or body["kv"] < 0:
        raise _shape(f"{name}: sealed value kv must be a non-negative integer")
    if not isinstance(body["r"], list) or not 0 < len(body["r"]) <= MAX_RECIPIENTS:
        raise _shape(f"{name}: a sealed value needs 1..{MAX_RECIPIENTS} recipients")
    for i, r in enumerate(body["r"]):
        what = f"{name}: r[{i}]"
        _keys(r, {"kind", "target", "epk", "ct"}, what)
        if r["kind"] not in ("device", "member") or not isinstance(r["target"], str) or not r["target"]:
            raise _shape(f"{what}: kind must be device or member, with a target")
        epk = r["epk"]
        if (not isinstance(epk, dict) or epk.get("kty") != "EC" or epk.get("crv") != "P-256"
                or len(b64u_bytes(epk.get("x", ""), f"{what}.epk.x")) != 32
                or len(b64u_bytes(epk.get("y", ""), f"{what}.epk.y")) != 32):
            raise _shape(f"{what}.epk must be a P-256 public JWK")
        if len(b64u_bytes(r["ct"], f"{what}.ct")) < NONCE_LEN + TAG_LEN:
            raise _shape(f"{what}.ct is shorter than a nonce and a tag")
    return value
