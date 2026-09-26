"""Whose page may a USER see? Their own — and only their own.

A page is a live picture of someone's screen. A person sees their own tabs; the
session driving a chat sees THAT chat's page through the chat key canopy issued
it (`tests/test_chat_keys.py`); a confined caller's session sees its own
conversation's through its caller token.

What changed (2026-09-26): an agent's LOGIN used to match every chat the agent
was in. That answered "which chat?" by inferring from an identity, and answered
it too broadly — every chat of the agent, not the one being driven. The chat key
replaced it, so these tests now pin that the login alone sees nothing.

Every test still makes the caller and the session's creator DIFFERENT people,
because that is the configuration that ships.
"""

from unittest import mock

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.canopy_sessions import chat_keys, page_access
from apps.canopy_sessions.models import Session
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _world():
    """A human, the agent they chat with, and a bystander agent in the same tenant."""
    human = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=human)
    WorkspaceMembership.objects.create(user=human, workspace=ws, role=WorkspaceMembership.OWNER)

    ace_user = User.objects.create_user("ace", "ace@dimagi-ai.com", "pw")
    ace = Agent.objects.create(slug="ace", name="Ace", workspace=ws, user=ace_user)

    hal_user = User.objects.create_user("hal", "hal@dimagi-ai.com", "pw")
    Agent.objects.create(slug="hal", name="Hal", workspace=ws, user=hal_user)

    session = Session.objects.create(workspace=ws, created_by=human, agent=ace, title="chat")
    return human, ace_user, hal_user, session


def _visible(user):
    return set(page_access.sessions_with_page_for(user).values_list("id", flat=True))


def _keyed(key):
    headers = {chat_keys.HEADER.lower(): key} if key else {}
    return mock.patch("apps.mcp.chat_scope.get_http_headers", return_value=headers)


# --- a person, their own tabs --------------------------------------------------


def test_the_human_sees_their_own_page():
    human, _ace, _hal, session = _world()

    assert session.id in _visible(human)


def test_the_agents_login_alone_sees_nothing_not_even_its_own_chats():
    """The removed leg. An agent's login is in EVERY chat the agent is in, so it
    cannot say which chat is being driven; the chat key does."""
    _human, ace_user, _hal, session = _world()

    assert session.id not in _visible(ace_user)


def test_another_agent_or_a_stranger_sees_nothing():
    _human, _ace, hal_user, session = _world()
    stranger = User.objects.create_user("nope", "nope@dimagi.com", "pw")

    assert session.id not in _visible(hal_user)
    assert session.id not in _visible(stranger)


def test_no_caller_matches_nothing_rather_than_everything():
    """An empty `Q()` is the IDENTITY: returning one for an absent caller would
    widen the query to every session on the deployment."""
    _world()

    assert _visible(None) == set()
    assert _visible(object()) == set()
    assert _visible(User(pk=None)) == set()


def test_only_active_sessions_count():
    human, _ace, _hal, session = _world()
    session.status = Session.ARCHIVED
    session.save(update_fields=["status"])

    assert session.id not in _visible(human)


# --- the session driving a chat: its chat key -----------------------------------


def test_the_agent_sees_the_page_it_is_talking_about_through_the_chat_key():
    """Through the functions the MCP tools actually call — and only the keyed
    chat, even when the agent is in another chat with a page open."""
    from apps.canopy_sessions import page_actions, page_state
    from apps.mcp.page_tools import PageActionProvider
    from apps.mcp.tools.page import _page_of_chat
    from asgiref.sync import async_to_sync

    human, _ace, _hal, session = _world()
    other = Session.objects.create(workspace=session.workspace, created_by=human,
                                   agent=session.agent, title="another chat")
    for s, ids in ((session, [1, 2]), (other, [9])):
        page_state.set_page_state(s, {"path": "/insights", "visible_ids": ids})
    page_actions.set_declared_actions(session, [{"name": "dismissInsights"}])
    page_actions.set_declared_actions(other, [{"name": "somethingElse"}])
    key = chat_keys.mint(session)

    with _keyed(key):
        pages = _page_of_chat()
        tools = async_to_sync(PageActionProvider()._list_tools)()

    assert [p["state"]["visible_ids"] for p in pages] == [[1, 2]]
    assert [t.name for t in tools] == ["page_dismissInsights"]


def test_without_a_key_the_agent_gets_no_page_tools():
    from apps.canopy_sessions import page_actions
    from apps.mcp.page_tools import page_tool_specs

    _human, ace_user, _hal, session = _world()
    page_actions.set_declared_actions(session, [{"name": "dismissInsights"}])

    assert page_tool_specs(ace_user) == []
