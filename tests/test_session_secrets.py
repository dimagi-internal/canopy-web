"""Secrets shared with a chat (`models.SessionSecret`).

The contract: a person hands a value to one chat without it entering the chat,
and ONLY the session bound to that chat can spend it. The agent side names
itself by its Claude session id (RunnerBinding.transcript_id); there is no route
that takes a chat id. Values never come back to a browser, and a secret dies 30
minutes after it was shared.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import secrets as session_secrets
from apps.canopy_sessions.models import Message, RunnerBinding, Session, SessionSecret
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

TOKEN = "ghp_not_a_real_token_0123456789"
MINE = "eb742bd8-0000-0000-0000-00000000000a"    # this conversation's Claude session id
OTHER = "eb742bd8-0000-0000-0000-00000000000b"   # another conversation of the same agent


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
    runner = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", paired_by=owner, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )

    def chat(transcript_id, key):
        s = Session.objects.create(workspace=ws, created_by=owner, origin=Session.ORIGIN_WEB, agent=agent)
        RunnerBinding.objects.create(session=s, runner=runner, session_key=key,
                                     emdash_project="hal", transcript_id=transcript_id)
        return s

    session = chat(MINE, "task-a")
    other = chat(OTHER, "task-b")
    browser = Client()
    browser.force_login(owner)
    return {"owner": owner, "ws": ws, "bot": bot, "agent": agent, "runner": runner,
            "session": session, "other": other, "browser": browser}


def _share(world, name="gh token", value=TOKEN, session=None):
    s = session or world["session"]
    return world["browser"].post(
        f"/api/canopy-sessions/{s.id}/secrets",
        data=json.dumps({"name": name, "value": value}),
        content_type="application/json",
    )


def _agent(world, user=None):
    return Client(HTTP_AUTHORIZATION=f"Bearer {_pat(user or world['bot'])}")


def _value(world, transcript=MINE, name="GH_TOKEN", user=None):
    return _agent(world, user).get(f"/api/session-secrets/{transcript}/{name}")


# ---- sharing -----------------------------------------------------------------

def test_sharing_stores_it_encrypted_and_posts_nothing_into_the_chat(world):
    r = _share(world)
    assert r.status_code == 201, r.content
    assert r.json()["name"] == "GH_TOKEN"
    assert TOKEN not in r.content.decode()
    assert TOKEN not in SessionSecret.objects.get(name="GH_TOKEN").value_enc
    assert not Message.objects.filter(session=world["session"]).exists()


def test_the_browser_list_shows_names_and_never_a_value(world):
    _share(world)
    r = world["browser"].get(f"/api/canopy-sessions/{world['session'].id}/secrets")
    assert [s["name"] for s in r.json()] == ["GH_TOKEN"]
    assert r.json()[0]["expires_at"]
    assert TOKEN not in r.content.decode()


def test_there_is_no_route_that_returns_a_value_by_chat_id(world):
    _share(world)
    r = _agent(world).get(f"/api/canopy-sessions/{world['session'].id}/secrets/GH_TOKEN/value")
    assert r.status_code in (404, 405)


def test_names_are_env_var_shaped(world):
    assert _share(world, name="my-token").json()["name"] == "MY_TOKEN"
    assert _share(world, name="9lives").status_code == 422
    assert _share(world, name="x", value="   ").status_code == 422


# ---- only the bound session --------------------------------------------------

def test_the_bound_session_lists_and_spends_its_secrets(world):
    _share(world)
    listed = _agent(world).get(f"/api/session-secrets/{MINE}")
    assert listed.status_code == 200
    assert [s["name"] for s in listed.json()] == ["GH_TOKEN"]
    assert TOKEN not in listed.content.decode()
    r = _value(world)
    assert r.status_code == 200
    assert r.json() == {"name": "GH_TOKEN", "value": TOKEN}
    assert SessionSecret.objects.get(name="GH_TOKEN").last_used_at is not None


def test_another_session_of_the_same_agent_cannot_see_or_spend_it(world):
    _share(world)
    assert _agent(world).get(f"/api/session-secrets/{OTHER}").json() == []
    assert _value(world, transcript=OTHER).status_code == 404


def test_each_session_sees_only_its_own_chats_secrets(world):
    _share(world, name="MINE_ONLY")
    _share(world, name="THEIRS_ONLY", session=world["other"])
    assert [s["name"] for s in _agent(world).get(f"/api/session-secrets/{MINE}").json()] == ["MINE_ONLY"]
    assert [s["name"] for s in _agent(world).get(f"/api/session-secrets/{OTHER}").json()] == ["THEIRS_ONLY"]


def test_an_unknown_session_gets_a_404(world):
    _share(world)
    assert _agent(world).get("/api/session-secrets/not-a-bound-session").status_code == 404


def test_knowing_the_session_id_is_not_enough_for_another_identity(world):
    _share(world)
    ada = User.objects.create_user("ada", "ada@dimagi-ai.com", "pw")
    Agent.objects.create(slug="ada", name="Ada", workspace=world["ws"], user=ada)
    assert _value(world, user=ada).status_code == 404
    co_tenant = User.objects.create_user("x", "x@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=co_tenant, workspace=world["ws"], role=WorkspaceMembership.EDITOR)
    assert _value(world, user=co_tenant).status_code == 404


def test_a_writer_of_the_chat_may_spend_it_from_its_session(world):
    _share(world)
    assert _value(world, user=world["owner"]).status_code == 200


def test_a_browser_session_is_refused_even_for_the_sharer(world):
    _share(world)
    assert world["browser"].get(f"/api/session-secrets/{MINE}/GH_TOKEN").status_code == 403


def test_forgetting_a_secret_removes_it(world):
    _share(world)
    r = world["browser"].delete(f"/api/canopy-sessions/{world['session'].id}/secrets/GH_TOKEN")
    assert r.status_code == 204
    assert _value(world).status_code == 404


# ---- 30-minute lifetime ------------------------------------------------------

def _age(minutes):
    SessionSecret.objects.update(updated_at=timezone.now() - dt.timedelta(minutes=minutes))


def test_an_expired_secret_is_refused_and_deleted(world):
    _share(world)
    _age(31)
    assert _value(world).status_code == 404
    assert not SessionSecret.objects.exists()


def test_a_secret_inside_its_window_still_works(world):
    _share(world)
    _age(29)
    assert _value(world).status_code == 200


def test_both_lists_drop_expired_secrets(world):
    _share(world)
    _age(31)
    assert world["browser"].get(f"/api/canopy-sessions/{world['session'].id}/secrets").json() == []
    _share(world)
    _age(31)
    assert _agent(world).get(f"/api/session-secrets/{MINE}").json() == []


def test_resharing_restarts_the_clock(world):
    _share(world)
    _age(29)
    _share(world, value="a-new-value-for-the-same-name")
    # 29 min after the FIRST share + 2 more would be past 30 had the clock not restarted.
    assert session_secrets.purge_expired(timezone.now() + dt.timedelta(minutes=2)) == 0
    assert _value(world).json()["value"] == "a-new-value-for-the-same-name"


def test_the_runner_heartbeat_sweeps_expired_secrets(world):
    from apps.harness import services as hsvc

    _share(world)
    _age(31)
    hsvc.heartbeat(world["runner"], active_turn_ids=[])
    assert not SessionSecret.objects.exists()
