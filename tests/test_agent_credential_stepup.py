"""Passkey step-up on agent credentials (canopy-web#1538).

- SETTING a value from a browser session needs a fresh step-up; a token (PAT /
  MCP) write does not — `canopy vault import` has no browser to ask.
- There is no reveal: the value is ciphertext canopy-web cannot open.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.passkeys.testing import give_vault_key, grant_step_up
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SECRET = "op-style-value-not-a-real-token"


@pytest.fixture
def fleet():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=jj,
                                 runtime_secrets=["nova-api-key"])
    give_vault_key(jj)  # someone to seal a value to: every write is zero-knowledge
    browser = Client()
    browser.force_login(jj)
    return {"jj": jj, "ws": ws, "agent": agent, "browser": browser}


def _pat(user) -> str:
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return raw


def _put(client, values, **extra):
    return client.put("/api/agents/eva/credentials", data={"values": values},
                      content_type="application/json", **extra)


# --- setting a value -------------------------------------------------------------

def test_a_browser_write_without_a_step_up_is_refused(fleet):
    from apps.agents.models import AgentCredential

    r = _put(fleet["browser"], {"nova-api-key": SECRET})
    assert r.status_code == 403
    body = r.json()
    assert body["error"] == "step_up_required"
    # No passkey yet, so the SPA should send them to register one.
    assert body["has_passkey"] is False and body["register_url"] == "/settings#passkeys"
    assert not AgentCredential.objects.exists()


def test_a_browser_write_with_a_step_up_lands(fleet):
    grant_step_up(fleet["browser"])
    assert _put(fleet["browser"], {"nova-api-key": SECRET}).status_code == 200


def test_an_expired_step_up_does_not_count(fleet):
    grant_step_up(fleet["browser"], seconds=-1)
    assert _put(fleet["browser"], {"nova-api-key": SECRET}).status_code == 403


def test_a_pat_write_needs_no_step_up(fleet):
    r = _put(Client(), {"nova-api-key": SECRET}, HTTP_AUTHORIZATION=f"Bearer {_pat(fleet['jj'])}")
    assert r.status_code == 200


def test_a_non_admin_is_refused_before_any_step_up_question(fleet):
    viewer = get_user_model().objects.create_user(username="v", email="v@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["ws"], user=viewer, role=WorkspaceMembership.VIEWER)
    c = Client()
    c.force_login(viewer)
    r = _put(c, {"nova-api-key": SECRET})
    assert r.status_code == 403
    assert r.json().get("error") != "step_up_required"
