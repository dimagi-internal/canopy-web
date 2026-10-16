"""Optional KMS envelope encryption for AgentCredential values (Canopy Vault).

canopy-web#1538 Phase 0. OFF unless ``settings.VAULT_KMS_KEY_ID`` is set, and
then only for NEW writes: every read handles both formats, so turning it on (or
off again) never strands a stored value.

FORMATS. A stored value is one of:

* a Fernet token (``apps/common/encryption.py``) — the default, and every value
  written before KMS was enabled;
* ``kms1:<header>.<wrapped>.<nonce>.<ciphertext>`` — each part urlsafe-base64
  without padding. ``header`` is the canonical JSON encryption context
  ``{"workspace", "agent", "name"}``; ``wrapped`` is the KMS-encrypted 256-bit
  data key (``GenerateDataKey`` under that context); ``nonce`` + ``ciphertext``
  are AES-256-GCM over the value, with the header as associated data.

WHY THE CONTEXT IS STORED. KMS refuses a decrypt whose context differs from the
encrypt's, so the context must be reproduced exactly. Rebuilding it from the row
breaks the moment an agent moves workspace (``move_agent``); storing it keeps the
value readable. The binding that matters — this ciphertext belongs to THIS
agent's credential of THIS name, so a row copied onto another agent or name does
not decrypt — is enforced by ``decrypt`` comparing the stored ``agent``/``name``
against the row it was read from. The header is also the GCM associated data, so
it cannot be edited without failing authentication, and KMS sees the same
context, so CloudTrail records which agent/credential each unwrap was for.

The KMS client is created lazily (``_client``) so importing this module needs no
AWS configuration, and tests substitute a stubbed client.
"""
from __future__ import annotations

import base64
import json
import os

from django.conf import settings

PREFIX = "kms1:"


class EnvelopeError(ValueError):
    """A ``kms1:`` value that is malformed or belongs to a different credential."""


def enabled() -> bool:
    return bool(getattr(settings, "VAULT_KMS_KEY_ID", ""))


def is_envelope(stored: str) -> bool:
    return (stored or "").startswith(PREFIX)


def _client():
    import boto3

    return boto3.client("kms", region_name=getattr(settings, "VAULT_KMS_REGION", None) or None)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _canonical(context: dict[str, str]) -> bytes:
    return json.dumps({k: str(v) for k, v in context.items()},
                      sort_keys=True, separators=(",", ":")).encode()


def encrypt(plaintext: str, *, context: dict[str, str], key_id: str | None = None) -> str:
    """Seal ``plaintext`` under a fresh data key from KMS. Empty in → empty out."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not plaintext:
        return ""
    ctx = {k: str(v) for k, v in context.items()}
    resp = _client().generate_data_key(
        KeyId=key_id or settings.VAULT_KMS_KEY_ID, KeySpec="AES_256", EncryptionContext=ctx,
    )
    header = _canonical(ctx)
    nonce = os.urandom(12)
    ct = AESGCM(resp["Plaintext"]).encrypt(nonce, plaintext.encode(), header)
    return PREFIX + ".".join(_b64e(p) for p in (header, resp["CiphertextBlob"], nonce, ct))


def decrypt(stored: str, *, expect: dict[str, str] | None = None) -> str:
    """Open a ``kms1:`` value. ``expect`` names the row's own ``agent``/``name``;
    a value whose stored context disagrees is refused (EnvelopeError)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not stored:
        return ""
    if not is_envelope(stored):
        raise EnvelopeError("not a kms1 envelope")
    try:
        header_b, wrapped, nonce, ct = (_b64d(p) for p in stored[len(PREFIX):].split("."))
        ctx = json.loads(header_b)
    except (ValueError, TypeError) as exc:
        raise EnvelopeError("malformed kms1 envelope") from exc
    if not isinstance(ctx, dict):
        raise EnvelopeError("malformed kms1 envelope header")
    for k, v in (expect or {}).items():
        if k == "workspace":
            continue  # an agent may move workspace; agent + name are the binding
        if str(ctx.get(k)) != str(v):
            raise EnvelopeError(f"kms1 envelope belongs to a different credential ({k})")
    # No KeyId: a symmetric KMS ciphertext names its own key, so a value written
    # under an earlier key (or with KMS since switched off) still opens.
    data_key = _client().decrypt(CiphertextBlob=wrapped, EncryptionContext=ctx)["Plaintext"]
    return AESGCM(data_key).decrypt(nonce, ct, header_b).decode()
