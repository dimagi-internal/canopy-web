"""Contract tests for apps/common/api.py (v2 common surface)."""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.common.schemas import MeOut

User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    return Client()


@pytest.fixture()
def user(db):
    return User.objects.create_user(
        username="tester",
        email="tester@dimagi.com",
        password="pass",
    )


@pytest.fixture()
def authed_client(client, user):
    client.force_login(user)
    return client


# ---------------------------------------------------------------------------
# 1. /health/ — public, no auth
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_health_public_no_auth(client):
    resp = client.get("/api/health/")
    assert resp.status_code == 200
    body = json.loads(resp.content)
    assert body == {"status": "ok"}


# ---------------------------------------------------------------------------
# 2. /me/ — authed
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_me_authed(authed_client):
    resp = authed_client.get("/api/me/")
    assert resp.status_code == 200
    body = json.loads(resp.content)
    parsed = MeOut.model_validate(body)
    assert parsed.email == "tester@dimagi.com"
    assert parsed.avatar_url == ""  # no social account attached


# ---------------------------------------------------------------------------
# 3. /me/ — anonymous → 401 + problem+json
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_me_anonymous_401(client):
    resp = client.get("/api/me/")
    assert resp.status_code == 401
    ct = resp.get("Content-Type", "")
    assert "problem+json" in ct or "json" in ct
    body = json.loads(resp.content)
    # RFC 7807 problem+json must have status field
    assert body.get("status") == 401
