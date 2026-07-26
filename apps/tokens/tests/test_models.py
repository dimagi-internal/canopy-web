"""Unit tests for the PersonalToken model."""
from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model

from apps.tokens.models import PersonalToken

User = get_user_model()


@pytest.mark.django_db
def test_create_returns_raw_and_persists_only_hash():
    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, token = PersonalToken.create_for_user(user=user, label="ci")
    # Raw is the token-urlsafe value (urlsafe-base64, no padding).
    assert isinstance(raw, str) and len(raw) >= 32
    # Only the hash is stored — the raw value never goes into the DB.
    assert token.token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert token.token_hash != raw
    # PersonalToken.objects can find the row by hash, never by raw.
    assert PersonalToken.objects.filter(token_hash=raw).count() == 0
    assert PersonalToken.objects.filter(token_hash=token.token_hash).count() == 1


@pytest.mark.django_db
def test_lookup_finds_active_token():
    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, token = PersonalToken.create_for_user(user=user, label="ci")
    found = PersonalToken.lookup(raw)
    assert found is not None
    assert found.pk == token.pk
    assert found.user_id == user.pk


@pytest.mark.django_db
def test_lookup_miss_returns_none():
    assert PersonalToken.lookup("nonexistent-raw") is None
    assert PersonalToken.lookup("") is None


@pytest.mark.django_db
def test_lookup_ignores_revoked():
    from django.utils import timezone

    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, token = PersonalToken.create_for_user(user=user, label="ci")
    assert PersonalToken.lookup(raw) is not None
    PersonalToken.objects.filter(pk=token.pk).update(revoked_at=timezone.now())
    assert PersonalToken.lookup(raw) is None


@pytest.mark.django_db
def test_is_active_property():
    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    _, token = PersonalToken.create_for_user(user=user, label="ci")
    assert token.is_active is True
    from django.utils import timezone

    token.revoked_at = timezone.now()
    assert token.is_active is False


@pytest.mark.django_db
def test_two_tokens_have_different_raws():
    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw1, _ = PersonalToken.create_for_user(user=user, label="a")
    raw2, _ = PersonalToken.create_for_user(user=user, label="b")
    assert raw1 != raw2


# ── Expiry ──────────────────────────────────────────────────────────────────────
# `lookup()` is the enforcement point for BOTH bearer auth and MCP, so these
# assert on lookup, not on is_active. is_active is display-only and is asserted
# separately to stay in agreement with lookup — a divergence between the two is
# the specific bug this design set out to avoid.


@pytest.mark.django_db
def test_mint_defaults_to_settings_ttl():
    from django.conf import settings
    from django.utils import timezone

    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    _, token = PersonalToken.create_for_user(user=user, label="ci")
    assert token.expires_at is not None
    delta = token.expires_at - timezone.now()
    assert abs(delta.days - settings.PAT_DEFAULT_TTL_DAYS) <= 1


@pytest.mark.django_db
def test_mint_with_explicit_ttl():
    from django.utils import timezone

    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    _, token = PersonalToken.create_for_user(user=user, label="ci", ttl_days=30)
    assert abs((token.expires_at - timezone.now()).days - 30) <= 1


@pytest.mark.django_db
def test_ttl_zero_never_expires():
    """0 is the documented 'never' sentinel — same rule on the API and the CLI."""
    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, token = PersonalToken.create_for_user(user=user, label="forever", ttl_days=0)
    assert token.expires_at is None
    assert PersonalToken.lookup(raw) is not None
    assert token.is_active is True


@pytest.mark.django_db
def test_negative_ttl_rejected():
    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    with pytest.raises(ValueError):
        PersonalToken.create_for_user(user=user, label="bad", ttl_days=-1)


@pytest.mark.django_db
def test_lookup_rejects_expired_token():
    from django.utils import timezone

    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, token = PersonalToken.create_for_user(user=user, label="ci", ttl_days=30)
    assert PersonalToken.lookup(raw) is not None
    PersonalToken.objects.filter(pk=token.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert PersonalToken.lookup(raw) is None


@pytest.mark.django_db
def test_lookup_grandfathers_null_expiry():
    """Pre-expiry rows carry expires_at=NULL and must keep authenticating —
    this is what migration 0005's missing data step guarantees."""
    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, token = PersonalToken.create_for_user(user=user, label="legacy")
    PersonalToken.objects.filter(pk=token.pk).update(expires_at=None)
    assert PersonalToken.lookup(raw) is not None


@pytest.mark.django_db
def test_is_active_agrees_with_lookup_on_expiry():
    from django.utils import timezone

    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, token = PersonalToken.create_for_user(user=user, label="ci", ttl_days=30)
    PersonalToken.objects.filter(pk=token.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    token.refresh_from_db()
    assert token.is_active is False
    assert PersonalToken.lookup(raw) is None
