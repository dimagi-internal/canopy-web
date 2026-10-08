"""Symmetric field encryption for secrets stored at rest (e.g. per-runner tokens).

A thin Fernet wrapper — proper authenticated symmetric crypto. Deliberately
service-layer (encrypt on write, decrypt on read) rather than a transparent model
field, to avoid coupling to a Django-version-specific field-encryption package; the
stored column is plain ciphertext text.

KEYS. A ``MultiFernet`` over an ordered key list:

1. the key derived from ``settings.FIELD_ENCRYPTION_KEY``, when it is set;
2. the LEGACY key derived from ``settings.SECRET_KEY`` — always present.

Writes encrypt with the FIRST key; reads try every key in order. So:

* ``FIELD_ENCRYPTION_KEY`` unset (dev, and production until it is provisioned) →
  the list is just the legacy key, exactly the single key this module used before
  (same derivation, same ciphertext format) — no behaviour change.
* ``FIELD_ENCRYPTION_KEY`` set → new rows are written under it, and every row
  written before it existed (under ``SECRET_KEY``) still decrypts. That is what
  makes the switch safe to deploy without a re-encryption step, and what lets a
  later re-encrypt pass (``MultiFernet.rotate``) move old rows across before the
  legacy key is dropped.

KEY DERIVATION. ``FIELD_ENCRYPTION_KEY`` is interpreted the same way ``SECRET_KEY``
always has been here: any string, run through SHA-256 and urlsafe-base64-encoded
into a 32-byte Fernet key. It is NOT a raw Fernet key. Kept for compatibility —
anything already encrypted under a ``FIELD_ENCRYPTION_KEY`` by the previous
one-key code (which used this exact derivation) still decrypts — and because it
accepts any high-entropy string, e.g. a Secrets Manager generated password.

Note the ordering consequence: until ``SECRET_KEY``-encrypted rows are re-encrypted,
``SECRET_KEY`` remains able to decrypt them, so rotating ``SECRET_KEY`` (or dropping
it from this list) before that re-encryption would make those rows unreadable.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings


def _derive_key(raw: str) -> bytes:
    # Fernet needs a 32-byte urlsafe-base64 key; derive one deterministically.
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def _keys() -> list[bytes]:
    """The ordered key list: the write key first, then every key reads may need."""
    keys: list[bytes] = []
    field_key = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or ""
    if field_key:
        keys.append(_derive_key(field_key))
    legacy = _derive_key(settings.SECRET_KEY)
    if legacy not in keys:
        keys.append(legacy)
    return keys


def _fernet() -> MultiFernet:
    return MultiFernet([Fernet(k) for k in _keys()])


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret for storage (under the first key). Empty in → empty out."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored secret, trying every configured key. Empty in → empty out."""
    if not ciphertext:
        return ""
    return _fernet().decrypt(ciphertext.encode()).decode()
