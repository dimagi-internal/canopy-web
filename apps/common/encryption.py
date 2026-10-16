"""Symmetric field encryption for secrets stored at rest (e.g. per-runner tokens).

A thin Fernet wrapper — proper authenticated symmetric crypto. Deliberately
service-layer (encrypt on write, decrypt on read) rather than a transparent model
field, to avoid coupling to a Django-version-specific field-encryption package; the
stored column is plain ciphertext text.

KEYS. A ``MultiFernet`` over an ordered key list (see ``_keys``):

* ``FIELD_ENCRYPTION_KEY`` unset (dev, tests) → just the LEGACY key derived from
  ``settings.SECRET_KEY``.
* ``FIELD_ENCRYPTION_KEY`` set (production) → just the field key. The legacy key
  was dropped once agents 0053 had re-encrypted every row onto the field key, so
  ``SECRET_KEY`` no longer unlocks stored secrets. ``FIELD_ENCRYPTION_ACCEPT_LEGACY``
  re-adds it as a read key for an emergency.

Writes encrypt with the FIRST key; reads try every key in order.

KEY DERIVATION. ``FIELD_ENCRYPTION_KEY`` is interpreted the same way ``SECRET_KEY``
always has been here: any string, run through SHA-256 and urlsafe-base64-encoded
into a 32-byte Fernet key. It is NOT a raw Fernet key. Kept for compatibility —
anything already encrypted under a ``FIELD_ENCRYPTION_KEY`` by the previous
one-key code (which used this exact derivation) still decrypts — and because it
accepts any high-entropy string, e.g. a Secrets Manager generated password.

Rotating the field key: put the new value in ``FIELD_ENCRYPTION_KEY`` and the old
one in ``FIELD_ENCRYPTION_PREVIOUS_KEY`` (a read key), deploy, run
``reencrypt_secrets --apply``, then clear the previous key.
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
    """The ordered key list: the write key first, then every key reads may need.

    With ``FIELD_ENCRYPTION_KEY`` set, the legacy ``SECRET_KEY``-derived key is NOT
    in the list: production re-encrypted every row onto the field key (agents 0053,
    2026-10-16: 74 rewritten, 0 unreadable), so a leaked ``SECRET_KEY`` no longer
    decrypts anything. ``FIELD_ENCRYPTION_ACCEPT_LEGACY`` puts it back as a READ
    key — an emergency switch if a row written under it ever turns up.
    """
    field_key = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or ""
    legacy = _derive_key(settings.SECRET_KEY)
    if not field_key:
        return [legacy]
    keys = [_derive_key(field_key)]
    previous = getattr(settings, "FIELD_ENCRYPTION_PREVIOUS_KEY", "") or ""
    if previous and _derive_key(previous) not in keys:
        keys.append(_derive_key(previous))
    if getattr(settings, "FIELD_ENCRYPTION_ACCEPT_LEGACY", False) and legacy not in keys:
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
