"""Passkey step-up on agent credentials (canopy-web#1538).

- SETTING a value from a browser session needs a fresh step-up; a token (PAT /
  MCP) write does not — `canopy vault import` has no browser to ask.
- REVEAL is new: agent admin, browser session, step-up, returned once, recorded.
"""
from __future__ import annotations

import time

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.events.models import Event
from apps.passkeys import stepup
from apps.passkeys.testing import grant_step_up
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


def _reveal(client, name="nova-api-key", **extra):
    return client.post(f"/api/agents/eva/credentials/{name}/reveal", content_type="application/json", **extra)


def _store(agent, value=SECRET):
    from apps.agents import services

    services.set_agent_credentials(agent, {"nova-api-key": value})


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


# --- reveal ----------------------------------------------------------------------

def test_reveal_needs_a_step_up(fleet):
    _store(fleet["agent"])
    r = _reveal(fleet["browser"])
    assert r.status_code == 403 and r.json()["error"] == "step_up_required"
    assert SECRET not in r.content.decode()


def test_reveal_returns_the_value_once_and_records_who_saw_it(fleet):
    _store(fleet["agent"])
    grant_step_up(fleet["browser"])
    r = _reveal(fleet["browser"])
    assert r.status_code == 200, r.content
    assert r.json() == {"name": "nova-api-key", "value": SECRET}
    assert r["Cache-Control"] == "no-store"

    ev = Event.objects.get(kind="agent.credentials.revealed")
    assert ev.workspace_id == fleet["ws"].pk and ev.level == Event.WARN
    assert ev.payload["name"] == "nova-api-key" and ev.payload["by"] == "jj@dimagi.com"
    assert SECRET not in str(ev.payload) + ev.summary

    from apps.agents.models import AgentCredentialRead

    read = AgentCredentialRead.objects.get(agent=fleet["agent"], name="nova-api-key")
    assert read.via == "reveal" and read.user_id == fleet["jj"].pk


def test_every_reveal_is_its_own_record(fleet):
    _store(fleet["agent"])
    grant_step_up(fleet["browser"])
    _reveal(fleet["browser"])
    _reveal(fleet["browser"])
    assert Event.objects.filter(kind="agent.credentials.revealed").count() == 2


def test_a_reveal_that_cannot_be_recorded_does_not_happen(fleet, monkeypatch):
    from apps.events import services as events

    _store(fleet["agent"])
    grant_step_up(fleet["browser"])

    def boom(*a, **k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(events, "record", boom)
    r = _reveal(fleet["browser"])
    assert r.status_code == 500
    assert SECRET not in r.content.decode()


def test_a_token_cannot_reveal(fleet):
    _store(fleet["agent"])
    r = _reveal(Client(), HTTP_AUTHORIZATION=f"Bearer {_pat(fleet['jj'])}")
    assert r.status_code == 403 and r.json()["error"] == "browser_session_required"


def test_a_token_minted_session_cannot_reveal_even_if_marked_stepped_up(fleet):
    from apps.common.views_debug import DEBUG_SESSION_MARKER

    _store(fleet["agent"])
    c = fleet["browser"]
    s = c.session
    s[DEBUG_SESSION_MARKER] = True
    s[stepup.SESSION_KEY] = {"user": fleet["jj"].pk, "until": time.time() + 60}
    s.save()
    r = _reveal(c)
    assert r.status_code == 403 and r.json()["error"] == "browser_session_required"


def test_a_member_who_is_not_an_agent_admin_cannot_reveal(fleet):
    _store(fleet["agent"])
    editor = get_user_model().objects.create_user(username="ed", email="ed@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["ws"], user=editor, role=WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(editor)
    grant_step_up(c)
    r = _reveal(c)
    assert r.status_code == 403
    assert SECRET not in r.content.decode()
    assert not Event.objects.filter(kind="agent.credentials.revealed").exists()


def test_revealing_an_unset_name_is_a_404_and_records_nothing(fleet):
    grant_step_up(fleet["browser"])
    assert _reveal(fleet["browser"], name="nope").status_code == 404
    assert not Event.objects.filter(kind="agent.credentials.revealed").exists()
