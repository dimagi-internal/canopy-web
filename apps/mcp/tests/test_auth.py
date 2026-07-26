"""CanopyPATVerifier: a valid PAT resolves to its user; bad tokens 401."""
from __future__ import annotations

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.mcp.auth import CanopyPATVerifier
from apps.tokens.models import PersonalToken

User = get_user_model()


@pytest.mark.django_db
def test_valid_pat_resolves_to_user():
    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, _token = PersonalToken.create_for_user(user=user, label="cli")

    access = async_to_sync(CanopyPATVerifier().verify_token)(raw)

    assert access is not None
    assert access.claims["sub"] == str(user.pk)
    assert access.claims["user_id"] == user.pk
    assert access.claims["email"] == "alice@dimagi.com"
    assert access.client_id == str(user.pk)


@pytest.mark.django_db
def test_valid_pat_stamps_last_used():
    user = User.objects.create_user(username="bob", email="bob@dimagi.com")
    raw, token = PersonalToken.create_for_user(user=user, label="cli")
    assert token.last_used_at is None

    async_to_sync(CanopyPATVerifier().verify_token)(raw)

    token.refresh_from_db()
    assert token.last_used_at is not None


@pytest.mark.django_db
def test_missing_token_rejected():
    assert async_to_sync(CanopyPATVerifier().verify_token)("") is None


@pytest.mark.django_db
def test_invalid_token_rejected():
    assert async_to_sync(CanopyPATVerifier().verify_token)("not-a-real-token") is None


@pytest.mark.django_db
def test_revoked_token_rejected():
    user = User.objects.create_user(username="carol", email="carol@dimagi.com")
    raw, token = PersonalToken.create_for_user(user=user, label="cli")
    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])

    assert async_to_sync(CanopyPATVerifier().verify_token)(raw) is None


@pytest.mark.django_db
def test_expired_token_rejected():
    """MCP resolves PATs through PersonalToken.lookup(), so token expiry must
    reject here exactly as it does for bearer auth — asserted explicitly because
    MCP is the surface where a lookup regression would go unnoticed longest."""
    from datetime import timedelta

    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, token = PersonalToken.create_for_user(user=user, label="cli", ttl_days=30)
    assert async_to_sync(CanopyPATVerifier().verify_token)(raw) is not None

    PersonalToken.objects.filter(pk=token.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert async_to_sync(CanopyPATVerifier().verify_token)(raw) is None


@pytest.mark.django_db
def test_non_expiring_token_accepted():
    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, token = PersonalToken.create_for_user(user=user, label="cli", ttl_days=0)
    assert token.expires_at is None
    assert async_to_sync(CanopyPATVerifier().verify_token)(raw) is not None
