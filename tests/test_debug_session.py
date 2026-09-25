"""Tests for the debug-session mint endpoint."""
import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings


@pytest.fixture
def auth_client(db):
    user_model = get_user_model()
    user = user_model.objects.create_user(
        username="tester",
        email="tester@dimagi.com",
        password="irrelevant",
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def anon_client(db):
    return Client()


@override_settings(REQUIRE_AUTH=True)
def test_mint_requires_auth(anon_client):
    resp = anon_client.post("/api/debug/mint-session/")
    assert resp.status_code == 401


def test_mint_returns_cookie_shape(auth_client):
    resp = auth_client.post("/api/debug/mint-session/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cookie_name"] == "sessionid"
    assert data["cookie_value"]
    assert len(data["cookie_value"]) >= 16
    assert data["email"] == "tester@dimagi.com"
    assert data["ttl_seconds"] == 24 * 3600
    assert "curl" in data["curl_example"]
    assert data["cookie_value"] in data["curl_example"]


def test_mint_custom_ttl(auth_client):
    resp = auth_client.post(
        "/api/debug/mint-session/",
        data=json.dumps({"ttl_seconds": 3600}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.json()["ttl_seconds"] == 3600


def test_mint_ttl_clamped_upper(auth_client):
    resp = auth_client.post(
        "/api/debug/mint-session/",
        data=json.dumps({"ttl_seconds": 10_000_000}),
        content_type="application/json",
    )
    # Max is 1 week
    assert resp.json()["ttl_seconds"] == 7 * 24 * 3600


def test_mint_ttl_clamped_lower(auth_client):
    resp = auth_client.post(
        "/api/debug/mint-session/",
        data=json.dumps({"ttl_seconds": 1}),
        content_type="application/json",
    )
    # Min is 60 seconds
    assert resp.json()["ttl_seconds"] == 60


@override_settings(REQUIRE_AUTH=True)
def test_minted_cookie_authorizes_api(auth_client, db):
    """End-to-end: mint a cookie, then use it on a fresh client to hit a
    normally-gated endpoint. This is the whole point of the endpoint —
    if this doesn't work, the feature is broken."""
    mint = auth_client.post("/api/debug/mint-session/").json()

    fresh = Client()
    fresh.cookies["sessionid"] = mint["cookie_value"]
    resp = fresh.get("/api/projects/")
    assert resp.status_code == 200


def test_mint_session_is_marked_for_audit(auth_client):
    """Minted sessions carry a marker so they can be audited or bulk-revoked later."""
    from django.contrib.sessions.models import Session

    mint = auth_client.post("/api/debug/mint-session/").json()
    session = Session.objects.get(session_key=mint["cookie_value"])
    data = session.get_decoded()
    assert "_canopy_debug_session" in data
    assert data["_canopy_debug_session"]["minted_for_email"] == "tester@dimagi.com"


@override_settings(REQUIRE_AUTH=True)
def test_a_session_minted_from_a_token_cannot_make_browser_only_decisions(db):
    """Transferring an agent and changing its admins refuse any token, so a
    leaked one cannot take over an agent. Minting a cookie FROM a token must
    not be the way around that: the minted session is refused the same, while
    the same person signed in through the browser is not."""
    from apps.agents.models import Agent
    from apps.tokens.models import PersonalToken
    from apps.workspaces import services as wsvc
    from apps.workspaces.models import WorkspaceMembership
    from apps.workspaces.testing import a_workspace

    owner = get_user_model().objects.create_user("olive", "olive@dimagi.com", "pw")
    heir = get_user_model().objects.create_user("nina", "nina@dimagi.com", "pw")
    ws = a_workspace()
    wsvc.ensure_member(ws, owner, WorkspaceMembership.OWNER)
    wsvc.ensure_member(ws, heir, WorkspaceMembership.EDITOR)
    Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=owner)
    raw, _ = PersonalToken.create_for_user(user=owner, label="leaked")

    minted = Client().post("/api/debug/mint-session/", HTTP_AUTHORIZATION=f"Bearer {raw}").json()
    as_minted = Client()
    as_minted.cookies["sessionid"] = minted["cookie_value"]
    body = json.dumps({"user_id": heir.pk})
    r = as_minted.put("/api/agents/echo/owner", body, content_type="application/json")
    assert r.status_code == 403, r.content
    r = as_minted.put(f"/api/agents/echo/admins/{heir.pk}")
    assert r.status_code == 403, r.content
    assert Agent.objects.get(slug="echo").owner == owner

    person = Client()
    person.force_login(owner)
    r = person.put("/api/agents/echo/owner", body, content_type="application/json")
    assert r.status_code == 200, r.content
