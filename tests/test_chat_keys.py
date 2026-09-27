"""A chat key says "this request comes from THAT chat's session" (`ChatKey`).

It replaces an inference — "the caller is the chat's agent login and knows the
chat's Claude session id" — with a permission canopy issues: minted when a
runner claims a chat's turn, handed only to that runner, and reaching that
chat's secrets and page and nothing else.
"""
from __future__ import annotations

import datetime as dt
import json
from unittest import mock

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import chat_keys
from apps.canopy_sessions.models import ChatKey, Session
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.mcp import page_tools
from apps.mcp.tools import page as page_tool
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
    # The agent's login is NOT linked to the agent (Agent.user is None): the key
    # alone must be enough, which is the whole point.
    bot = User.objects.create_user("echo", "echo@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=bot, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=owner)

    def chat(**kw):
        return Session.objects.create(workspace=ws, created_by=owner, origin=Session.ORIGIN_WEB,
                                      agent=agent, **kw)

    browser = Client()
    browser.force_login(owner)
    return {"owner": owner, "ws": ws, "bot": bot, "agent": agent, "chat": chat,
            "browser": browser, "agent_client": Client(HTTP_AUTHORIZATION=f"Bearer {_pat(bot)}")}


def _share(world, session, name="GH_TOKEN", value=TOKEN):
    r = world["browser"].post(f"/api/canopy-sessions/{session.id}/secrets",
                              data=json.dumps({"name": name, "value": value}),
                              content_type="application/json")
    assert r.status_code == 201, r.content


# ---- issued at claim --------------------------------------------------------

def test_claiming_a_chats_turn_hands_the_runner_that_chats_key(world):
    session = world["chat"]()
    runner = Runner.objects.create(name="cloud", kind=Runner.CLOUD, paired_by=world["owner"],
                                   workspace=world["ws"], status=Runner.ONLINE,
                                   last_heartbeat_at=timezone.now(),
                                   capabilities={"sessions": True})
    RunnerAssignment.objects.create(agent=world["agent"], runner=runner, rank=0)
    Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                        prompt="hi", idempotency_key="k1", pinned_runner=runner)
    r = world["browser"].post(f"/api/harness/runners/{runner.pk}/claim")
    assert r.status_code == 200, r.content
    key = r.json()["chat_key"]
    assert key.startswith("chk_")
    assert chat_keys.resolve(key) == session
    assert not ChatKey.objects.filter(token_hash=key).exists()  # stored hashed, never raw


# ---- secrets ----------------------------------------------------------------

def test_the_key_reaches_its_chats_secrets_and_no_other_chats(world):
    mine, other = world["chat"](), world["chat"]()
    _share(world, mine)
    _share(world, other, value="ghp_the_other_chats_token_000000")
    key = chat_keys.mint(mine)
    c = world["agent_client"]

    names = c.get("/api/session-secrets/key", HTTP_X_CANOPY_CHAT_KEY=key)
    assert names.status_code == 200 and [s["name"] for s in names.json()] == ["GH_TOKEN"]
    value = c.get("/api/session-secrets/key/GH_TOKEN", HTTP_X_CANOPY_CHAT_KEY=key)
    assert value.json()["value"] == TOKEN  # this chat's value — not the other chat's


def test_no_key_a_bad_key_or_an_expired_key_gets_nothing(world):
    mine = world["chat"]()
    _share(world, mine)
    c = world["agent_client"]
    assert c.get("/api/session-secrets/key/GH_TOKEN").status_code == 404
    assert c.get("/api/session-secrets/key/GH_TOKEN", HTTP_X_CANOPY_CHAT_KEY="chk_nope").status_code == 404
    key = chat_keys.mint(mine)
    ChatKey.objects.update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    assert c.get("/api/session-secrets/key/GH_TOKEN", HTTP_X_CANOPY_CHAT_KEY=key).status_code == 404


def test_a_browser_session_is_never_given_a_value_even_with_the_key(world):
    mine = world["chat"]()
    _share(world, mine)
    key = chat_keys.mint(mine)
    r = world["browser"].get("/api/session-secrets/key/GH_TOKEN", HTTP_X_CANOPY_CHAT_KEY=key)
    assert r.status_code == 403
    assert TOKEN not in r.content.decode()


# ---- page context -------------------------------------------------------------

def _with_key(key):
    headers = {chat_keys.HEADER.lower(): key} if key else {}
    return mock.patch("apps.mcp.chat_scope.get_http_headers", return_value=headers)


def test_the_page_tools_answer_about_the_keyed_chat_only(world):
    """An agent's login is in every chat the agent is in; the key narrows the
    answer to the chat this session is driving."""
    action = {"name": "dismiss", "inputSchema": {"type": "object", "properties": {}}}
    mine = world["chat"](page_state={"version": 3, "visible_ids": [1, 2]},
                         page_actions_available=[action])
    world["chat"](page_state={"version": 9, "visible_ids": [99]},
                  page_actions_available=[{"name": "other"}])
    key = chat_keys.mint(mine)

    with _with_key(key):
        pages = page_tool._page_of_chat()
        tools = async_to_sync(page_tools.PageActionProvider()._list_tools)()
    assert [p["session_id"] for p in pages] == [str(mine.id)]
    assert [t.name for t in tools] == ["page_dismiss"]

    with _with_key(None):
        assert page_tool._page_of_chat() is None  # no key: the older paths answer
