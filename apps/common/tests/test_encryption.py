"""apps.common.encryption — MultiFernet key list (FIELD_ENCRYPTION_KEY, then legacy SECRET_KEY)."""
from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.fernet import Fernet, InvalidToken

from apps.common.encryption import decrypt_secret, encrypt_secret

SECRET = "django-test-secret-key-for-encryption"
FIELD = "a-separate-field-encryption-key"


def _legacy_ciphertext(raw_key: str, plaintext: str) -> str:
    """Ciphertext exactly as the pre-MultiFernet one-key code produced it."""
    key = base64.urlsafe_b64encode(hashlib.sha256(raw_key.encode()).digest())
    return Fernet(key).encrypt(plaintext.encode()).decode()


@pytest.fixture()
def legacy_only(settings):
    settings.SECRET_KEY = SECRET
    settings.FIELD_ENCRYPTION_KEY = ""
    return settings


@pytest.fixture()
def with_field_key(settings):
    settings.SECRET_KEY = SECRET
    settings.FIELD_ENCRYPTION_KEY = FIELD
    return settings


def test_roundtrip_with_field_key_unset(legacy_only):
    ct = encrypt_secret("tok-unset")
    assert ct != "tok-unset"
    assert decrypt_secret(ct) == "tok-unset"


def test_unset_is_the_old_single_key(legacy_only):
    # Both directions with the pre-change derivation: old rows read, new rows are
    # readable by the old code (a rollback stays safe).
    assert decrypt_secret(_legacy_ciphertext(SECRET, "old-row")) == "old-row"
    ct = encrypt_secret("new-row")
    old_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET.encode()).digest()))
    assert old_fernet.decrypt(ct.encode()).decode() == "new-row"


def test_roundtrip_with_field_key_set(with_field_key):
    ct = encrypt_secret("tok-set")
    assert decrypt_secret(ct) == "tok-set"


def test_legacy_ciphertext_still_decrypts_with_field_key_set(with_field_key):
    assert decrypt_secret(_legacy_ciphertext(SECRET, "pre-existing")) == "pre-existing"


def test_ciphertext_previously_made_under_field_key_still_decrypts(with_field_key):
    # The old code, with FIELD_ENCRYPTION_KEY set, used the same sha256 derivation.
    assert decrypt_secret(_legacy_ciphertext(FIELD, "field-era")) == "field-era"


def test_writes_use_the_field_key_not_the_legacy_one(settings):
    settings.SECRET_KEY = SECRET
    settings.FIELD_ENCRYPTION_KEY = FIELD
    ct = encrypt_secret("written-under-field-key")

    settings.FIELD_ENCRYPTION_KEY = ""  # legacy-only configuration
    with pytest.raises(InvalidToken):
        decrypt_secret(ct)


def test_empty_in_empty_out(with_field_key):
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""


def test_empty_in_empty_out_unset(legacy_only):
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""
