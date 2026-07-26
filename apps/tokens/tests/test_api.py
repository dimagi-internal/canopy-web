"""Contract tests for /api/tokens/ endpoints."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.tokens.models import PersonalToken
from apps.tokens.schemas import PersonalTokenCreatedOut, PersonalTokenOut

User = get_user_model()


@pytest.fixture
def authed(db, client):
    user = User.objects.create_user(username="alice", email="alice@dimagi.com", password="pw")
    client.force_login(user)
    return client, user


@pytest.mark.django_db
def test_list_tokens_empty(authed):
    client, _ = authed
    response = client.get("/api/tokens/")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.django_db
def test_list_tokens_only_mine(authed):
    """Tokens are scoped to request.user. Another user's tokens never leak."""
    client, user = authed
    other = User.objects.create_user(username="bob", email="bob@dimagi.com")
    PersonalToken.create_for_user(user=user, label="mine")
    PersonalToken.create_for_user(user=other, label="not mine")

    response = client.get("/api/tokens/")
    body = response.json()
    assert response.status_code == 200
    assert len(body) == 1
    assert body[0]["label"] == "mine"
    [PersonalTokenOut.model_validate(item) for item in body]


@pytest.mark.django_db
def test_create_token(authed):
    client, _ = authed
    response = client.post(
        "/api/tokens/",
        data='{"label": "ci-bootstrap"}',
        content_type="application/json",
    )
    assert response.status_code == 201
    body = response.json()
    parsed = PersonalTokenCreatedOut.model_validate(body)
    assert parsed.label == "ci-bootstrap"
    assert isinstance(parsed.raw, str) and len(parsed.raw) >= 32


@pytest.mark.django_db
def test_create_token_rejects_empty_label(authed):
    client, _ = authed
    response = client.post(
        "/api/tokens/",
        data='{"label": ""}',
        content_type="application/json",
    )
    assert response.status_code == 422
    assert response["Content-Type"].startswith("application/problem+json")


@pytest.mark.django_db
def test_revoke_token(authed):
    client, user = authed
    _, token = PersonalToken.create_for_user(user=user, label="t1")
    response = client.delete(f"/api/tokens/{token.pk}/")
    assert response.status_code == 204
    token.refresh_from_db()
    assert token.revoked_at is not None


@pytest.mark.django_db
def test_revoke_idempotent(authed):
    client, user = authed
    _, token = PersonalToken.create_for_user(user=user, label="t1")
    client.delete(f"/api/tokens/{token.pk}/")
    response = client.delete(f"/api/tokens/{token.pk}/")
    assert response.status_code == 204  # second DELETE is a no-op


@pytest.mark.django_db
def test_revoke_other_users_token_404(authed):
    """Can't revoke someone else's token (404 hides existence)."""
    client, _ = authed
    other = User.objects.create_user(username="bob", email="bob@dimagi.com")
    _, token = PersonalToken.create_for_user(user=other, label="theirs")
    response = client.delete(f"/api/tokens/{token.pk}/")
    assert response.status_code == 404
    token.refresh_from_db()
    assert token.revoked_at is None


@pytest.mark.django_db
def test_revoke_nonexistent_404(authed):
    client, _ = authed
    response = client.delete("/api/tokens/999999/")
    assert response.status_code == 404
    assert response["Content-Type"].startswith("application/problem+json")


@pytest.mark.django_db
def test_anonymous_cannot_list(db, client):
    response = client.get("/api/tokens/")
    assert response.status_code == 401


@pytest.mark.django_db
def test_bearer_can_authenticate_then_list_own_tokens(db, client):
    """End-to-end: mint a token, use it to authenticate, see only that user's tokens."""
    user = User.objects.create_user(username="alice", email="alice@dimagi.com")
    raw, _ = PersonalToken.create_for_user(user=user, label="self")
    response = client.get("/api/tokens/", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["label"] == "self"


@pytest.mark.django_db
def test_create_token_defaults_to_expiring(authed):
    """A plain mint gets the server default TTL — expiry is opt-out, not opt-in."""
    client, _ = authed
    response = client.post(
        "/api/tokens/", data='{"label": "ci"}', content_type="application/json"
    )
    assert response.status_code == 201
    assert PersonalTokenCreatedOut.model_validate(response.json()).expires_at is not None


@pytest.mark.django_db
def test_create_token_honors_ttl_days(authed):
    from django.utils import timezone

    client, _ = authed
    response = client.post(
        "/api/tokens/",
        data='{"label": "short", "ttl_days": 7}',
        content_type="application/json",
    )
    assert response.status_code == 201
    parsed = PersonalTokenCreatedOut.model_validate(response.json())
    assert abs((parsed.expires_at - timezone.now()).days - 7) <= 1


@pytest.mark.django_db
def test_create_token_ttl_zero_never_expires(authed):
    client, _ = authed
    response = client.post(
        "/api/tokens/",
        data='{"label": "forever", "ttl_days": 0}',
        content_type="application/json",
    )
    assert response.status_code == 201
    assert PersonalTokenCreatedOut.model_validate(response.json()).expires_at is None


@pytest.mark.django_db
def test_create_token_rejects_negative_ttl(authed):
    client, _ = authed
    response = client.post(
        "/api/tokens/",
        data='{"label": "bad", "ttl_days": -1}',
        content_type="application/json",
    )
    assert response.status_code == 422
    assert response["Content-Type"].startswith("application/problem+json")
