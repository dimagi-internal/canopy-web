"""An embedded widget never receives the agent's tool calls, and cannot ask to.

A widget's visitor asked a question and wants the answer; which MCP tools ran is
noise to them. So the rule is decided by the CREDENTIAL — a site's delegated
token or a contact token — on every path a widget reads a conversation through:
the live socket (`test_agui_socket.py` covers the frames), the socket's auth,
and REST history. canopy's own chat page, signed in as the person, still gets
every call.

Also here: a widget names an earlier conversation by what was first asked
(`opening`), because the session title is canopy's and means nothing to them.
"""
from __future__ import annotations

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions import services
from apps.canopy_sessions.api import EMBED_APP_KEY
from apps.canopy_sessions.models import Message, Session
from apps.realtime.channels_auth import RealtimeAuthMiddleware
from apps.tokens.models import AppCredential, AppCredentialAgent, DelegatedToken, PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    me = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=me)
    WorkspaceMembership.objects.create(user=me, workspace=ws, role=WorkspaceMembership.OWNER)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws)
    site = AppCredential.create_credential(name="connect-labs", created_by=me, workspace=ws)
    AppCredentialAgent.objects.create(app=site, agent=ace)
    raw, _ = DelegatedToken.issue(app=site, user=me, ttl_seconds=600)
    session = Session.objects.create(
        workspace_id=ws.pk, created_by=me, agent=ace, title="cw-3f9a-thread",
        metadata={EMBED_APP_KEY: site.name},
    )
    rows = [
        (Message.USER, "Which orgs are in Kenya?\n\n[page context: 245 org slugs]"),
        (Message.TOOL_USE, ""),
        (Message.TOOL_RESULT, ""),
        (Message.ASSISTANT, "Twenty-three of them."),
    ]
    for i, (role, text) in enumerate(rows):
        Message.objects.create(session=session, turn_index=i, role=role, plaintext=text)
    own, _ = PersonalToken.create_for_user(user=me, label="own")
    return {"me": me, "session": session, "raw": raw,
            "widget": {"HTTP_AUTHORIZATION": f"Bearer {raw}"},
            "own": {"HTTP_AUTHORIZATION": f"Bearer {own}"}}


def _roles(messages):
    return [m["role"] for m in messages]


def test_widget_history_over_rest_has_no_tool_rows(world):
    sid = world["session"].id
    c = Client()

    detail = c.get(f"/api/canopy-sessions/{sid}", **world["widget"]).json()
    older = c.get(f"/api/canopy-sessions/{sid}/messages?before=99", **world["widget"]).json()

    assert _roles(detail["messages"]) == ["user", "assistant"]
    assert _roles(older["messages"]) == ["user", "assistant"]


def test_the_person_on_canopy_itself_still_sees_every_call(world):
    sid = world["session"].id
    detail = Client().get(f"/api/canopy-sessions/{sid}", **world["own"]).json()

    assert _roles(detail["messages"]) == ["user", "tool_use", "tool_result", "assistant"]


def _ws_scope(query: str, *, login=None):
    captured = {}

    async def app(scope, receive, send):
        captured.update(scope)

    headers = []
    if login is not None:
        # A same-origin embed: canopy's own session cookie rides the socket.
        c = Client()
        c.force_login(login)
        headers.append((b"cookie", f"sessionid={c.cookies['sessionid'].value}".encode()))
    scope = {"type": "websocket", "path": f"/ws/canopy-sessions/{'0' * 32}/",
             "query_string": query.encode(), "headers": headers}
    async_to_sync(RealtimeAuthMiddleware(app))(scope, None, None)
    return captured


def test_a_widget_token_marks_the_socket_as_a_widgets(world):
    assert _ws_scope(f"token={world['raw']}")["via_widget"] is True


def test_canopy_embedding_its_own_widget_is_still_a_widget(world):
    """Same-origin: the cookie signs the socket in, so `delegated_app` is never
    resolved — the site token riding along is what says this is a widget."""
    scope = _ws_scope(f"token={world['raw']}", login=world["me"])

    assert scope["user"].pk == world["me"].pk
    assert scope["via_widget"] is True


def test_canopys_own_chat_page_socket_is_not_a_widget(world):
    assert _ws_scope("", login=world["me"])["via_widget"] is False


def test_a_widget_names_a_conversation_by_what_was_asked(world):
    [row] = Client().get("/api/canopy-sessions/?state=all", **world["widget"]).json()

    # The question, without the page-context block the widget appended to it.
    assert row["opening"] == "Which orgs are in Kenya?"


def test_a_long_opening_is_trimmed():
    class S:
        _opening = "word " * 100

    text = services.opening_of(S())
    assert len(text) <= services.OPENING_CHARS
    assert text.endswith("…")
