"""Secrets shared with a chat by reference (`models.SessionSecret`).

The contract: a person hands a value to one chat without it entering the chat.
So the value never comes back to a browser, never lands in a Message, and comes
out as plaintext only to a bearer who could act in the chat anyway — or who IS
the chat's agent, which is the caller that actually spends it.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions.models import Message, Session, SessionSecret
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

TOKEN = "ghp_not_a_real_token_0123456789"


def _pat(user) -> str:
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return raw


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    bot = User.objects.create_user("hal", "hal@dimagi-ai.com", "pw")
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, user=bot)
    session = Session.objects.create(workspace=ws, created_by=owner, origin=Session.ORIGIN_WEB, agent=agent)
    browser = Client()
    browser.force_login(owner)
    return {"owner": owner, "ws": ws, "bot": bot, "agent": agent, "session": session, "browser": browser}


def _share(world, name="gh token", value=TOKEN, note=""):
    return world["browser"].post(
        f"/api/canopy-sessions/{world['session'].id}/secrets",
        data=json.dumps({"name": name, "value": value, "note": note}),
        content_type="application/json",
    )


def _value_url(world, name="GH_TOKEN"):
    return f"/api/canopy-sessions/{world['session'].id}/secrets/{name}/value"


def test_sharing_stores_it_encrypted_and_returns_a_reference_not_the_value(world):
    r = _share(world, note="put it in canopy's Actions secrets")
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["name"] == "GH_TOKEN"
    assert body["ref"] == f"canopy-secret://{world['session'].id}/GH_TOKEN"
    assert TOKEN not in r.content.decode()
    assert body["ref"] in body["message"] and "canopy secret exec" in body["message"]
    assert "put it in canopy's Actions secrets" in body["message"]
    row = SessionSecret.objects.get(session=world["session"], name="GH_TOKEN")
    assert TOKEN not in row.value_enc


def test_sharing_writes_nothing_into_the_chat(world):
    _share(world)
    assert not Message.objects.filter(session=world["session"]).exists()


def test_the_list_never_carries_a_value(world):
    _share(world)
    r = world["browser"].get(f"/api/canopy-sessions/{world['session'].id}/secrets")
    assert r.status_code == 200
    assert [s["name"] for s in r.json()] == ["GH_TOKEN"]
    assert TOKEN not in r.content.decode()


def test_a_browser_session_is_refused_the_value_even_for_the_sharer(world):
    _share(world)
    assert world["browser"].get(_value_url(world)).status_code == 403


def test_the_sessions_own_agent_gets_the_value_with_its_pat(world):
    """The agent's identity is not a participant of the chat it runs in; this
    leg is what lets the one doing the work spend the secret."""
    _share(world)
    r = Client(HTTP_AUTHORIZATION=f"Bearer {_pat(world['bot'])}").get(_value_url(world))
    assert r.status_code == 200, r.content
    assert r.json() == {"name": "GH_TOKEN", "value": TOKEN}
    assert SessionSecret.objects.get(name="GH_TOKEN").last_used_at is not None


def test_a_writer_of_the_session_gets_the_value_with_a_pat(world):
    _share(world)
    r = Client(HTTP_AUTHORIZATION=f"Bearer {_pat(world['owner'])}").get(_value_url(world))
    assert r.status_code == 200


def test_a_co_tenant_who_cannot_act_in_the_chat_gets_a_404(world):
    _share(world)
    other = User.objects.create_user("x", "x@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=other, workspace=world["ws"], role=WorkspaceMembership.EDITOR)
    r = Client(HTTP_AUTHORIZATION=f"Bearer {_pat(other)}").get(_value_url(world))
    assert r.status_code == 404


def test_another_agents_identity_gets_a_404(world):
    _share(world)
    ada = User.objects.create_user("ada", "ada@dimagi-ai.com", "pw")
    Agent.objects.create(slug="ada", name="Ada", workspace=world["ws"], user=ada)
    r = Client(HTTP_AUTHORIZATION=f"Bearer {_pat(ada)}").get(_value_url(world))
    assert r.status_code == 404


def test_names_are_env_var_shaped(world):
    assert _share(world, name="my-token").json()["name"] == "MY_TOKEN"
    assert _share(world, name="9lives").status_code == 422
    assert _share(world, name="x", value="   ").status_code == 422


def test_forgetting_a_secret_removes_it(world):
    _share(world)
    r = world["browser"].delete(f"/api/canopy-sessions/{world['session'].id}/secrets/GH_TOKEN")
    assert r.status_code == 204
    r = Client(HTTP_AUTHORIZATION=f"Bearer {_pat(world['owner'])}").get(_value_url(world))
    assert r.status_code == 404


# ---- 30-minute lifetime ------------------------------------------------------
# A shared secret is a hand-off: it is deleted 30 minutes after it was shared,
# refused on read the moment it expires, and swept on the runner heartbeat so
# the ciphertext does not linger in a chat nobody opens again.


def _age(minutes):
    import datetime as dt

    from django.utils import timezone

    SessionSecret.objects.update(updated_at=timezone.now() - dt.timedelta(minutes=minutes))


def test_a_secret_says_when_it_expires(world):
    body = _share(world).json()
    assert "30 minutes" in body["message"]
    assert body["expires_at"]


def test_an_expired_secret_is_refused_and_deleted(world):
    _share(world)
    _age(31)
    r = Client(HTTP_AUTHORIZATION=f"Bearer {_pat(world['bot'])}").get(_value_url(world))
    assert r.status_code == 404
    assert not SessionSecret.objects.exists()


def test_a_secret_inside_its_window_still_works(world):
    _share(world)
    _age(29)
    r = Client(HTTP_AUTHORIZATION=f"Bearer {_pat(world['bot'])}").get(_value_url(world))
    assert r.status_code == 200


def test_the_list_drops_expired_secrets(world):
    _share(world)
    _age(31)
    r = world["browser"].get(f"/api/canopy-sessions/{world['session'].id}/secrets")
    assert r.json() == []


def test_resharing_restarts_the_clock(world):
    _share(world)
    _age(29)
    _share(world, value="a-new-value-for-the-same-name")  # restarts the 30 minutes
    import datetime as dt

    from apps.canopy_sessions import secrets as session_secrets
    from django.utils import timezone

    # 29 min after the FIRST share + 2 more would be past 30 if the clock had not restarted.
    assert session_secrets.purge_expired(timezone.now() + dt.timedelta(minutes=2)) == 0
    r = Client(HTTP_AUTHORIZATION=f"Bearer {_pat(world['bot'])}").get(_value_url(world))
    assert r.json()["value"] == "a-new-value-for-the-same-name"


def test_the_runner_heartbeat_sweeps_expired_secrets(world):
    from django.utils import timezone

    from apps.harness import services as hsvc
    from apps.harness.models import Runner

    _share(world)
    _age(31)
    runner = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", paired_by=world["owner"],
        workspace=world["ws"], status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    hsvc.heartbeat(runner, active_turn_ids=[])
    assert not SessionSecret.objects.exists()
