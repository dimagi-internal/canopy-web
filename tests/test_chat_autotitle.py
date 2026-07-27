"""Task 10 — server-side auto-title from the first user message.

`maybe_autotitle` is exercised directly (unit-level) plus once through the real
`turn_events_appended` signal path (append_events -> on_turn_events -> the
session.title_updated realtime frame), so the wiring in apps.py::ready() and
the receiver's assistant-event gate are both proven, not just the pure function.
"""
from __future__ import annotations

import pytest
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.canopy_sessions import autotitle
from apps.canopy_sessions.models import Message, Session
from apps.harness import services as harness_services
from apps.harness.models import Turn
from apps.realtime import groups
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture()
def ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    return user, ws, agent


def test_autotitle_from_first_user_message(ctx):
    user, ws, agent = ctx
    session = Session.objects.create(workspace=ws, agent=agent, created_by=user, title="")
    Message.objects.create(
        session=session, turn_index=0, role="user", content={},
        plaintext="Help me plan the Q3 field visit schedule",
    )
    title = autotitle.maybe_autotitle(session.pk)
    session.refresh_from_db()
    assert session.title == title == "Help me plan the Q3 field visit schedule"


def test_autotitle_never_overwrites(ctx):
    user, ws, agent = ctx
    session = Session.objects.create(workspace=ws, agent=agent, created_by=user, title="Existing")
    Message.objects.create(session=session, turn_index=0, role="user", content={}, plaintext="hello")
    assert autotitle.maybe_autotitle(session.pk) is None
    session.refresh_from_db()
    assert session.title == "Existing"


def test_autotitle_noop_with_no_user_message(ctx):
    user, ws, agent = ctx
    session = Session.objects.create(workspace=ws, agent=agent, created_by=user, title="")
    assert autotitle.maybe_autotitle(session.pk) is None
    session.refresh_from_db()
    assert session.title == ""


def test_autotitle_truncates_and_collapses_whitespace(ctx):
    user, ws, agent = ctx
    session = Session.objects.create(workspace=ws, agent=agent, created_by=user, title="")
    long_text = "line one\n  line two   with   extra   spaces " + ("x" * 100)
    Message.objects.create(session=session, turn_index=0, role="user", content={}, plaintext=long_text)
    title = autotitle.maybe_autotitle(session.pk)
    assert title is not None
    assert len(title) == autotitle.TITLE_MAX
    assert "\n" not in title
    assert "  " not in title


def test_assistant_turn_event_triggers_autotitle_and_publishes(ctx):
    """The full wire: appending an assistant TurnEvent on a chat-session turn
    fires the harness signal, apps.py's connected receiver runs maybe_autotitle,
    and the session's realtime group gets a session.title_updated frame."""
    user, ws, agent = ctx
    session = Session.objects.create(workspace=ws, agent=agent, created_by=user, title="")
    Message.objects.create(
        session=session, turn_index=0, role="user", content={}, plaintext="hello there",
    )
    turn = Turn.objects.create(
        chat_session=session, origin=Turn.ORIGIN_API, idempotency_key="autotitle-1",
    )
    layer = get_channel_layer()
    async_to_sync(layer.group_add)(groups.session_group(session.id), "autotitle-chan")

    harness_services.append_events(turn, [{"kind": "assistant", "payload": {"text": "hi!"}}])

    # realtime's own receiver also fans this same signal out as a chat.turn_event
    # frame on the same group, so pull frames until the title one shows up.
    msg = None
    for _ in range(5):
        candidate = async_to_sync(layer.receive)("autotitle-chan")
        if candidate["type"] == "session.title_updated":
            msg = candidate
            break
    assert msg is not None, "session.title_updated frame never arrived"
    assert msg["title"] == "hello there"
    session.refresh_from_db()
    assert session.title == "hello there"


def test_non_assistant_turn_event_does_not_autotitle(ctx):
    user, ws, agent = ctx
    session = Session.objects.create(workspace=ws, agent=agent, created_by=user, title="")
    Message.objects.create(
        session=session, turn_index=0, role="user", content={}, plaintext="hello there",
    )
    turn = Turn.objects.create(
        chat_session=session, origin=Turn.ORIGIN_API, idempotency_key="autotitle-2",
    )
    harness_services.append_events(turn, [{"kind": "tool_start", "payload": {}}])
    session.refresh_from_db()
    assert session.title == ""


def test_a_runner_backed_session_keeps_the_emdash_task_name():
    """The name the human sees in their own emdash sidebar is what they navigate
    by. Autotitling over it made the session HARDER to find — observed
    2026-07-27: "I think we basically implemented everything we need to…" where
    the sidebar said "canopy-web-api-7716-0726-1521"."""
    from apps.canopy_sessions.models import RunnerBinding
    from apps.harness.models import Runner

    user = User.objects.create_user("t1", "t1@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w-title", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws,
                                       role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   paired_by=user)
    session = Session.objects.create(workspace=ws, created_by=user, title="")
    RunnerBinding.objects.create(session=session, runner=runner,
                                 session_key="canopy-web-api-7716")
    Message.objects.create(session=session, turn_index=0, role=Message.USER,
                           plaintext="I think we basically implemented everything")

    assert autotitle.maybe_autotitle(session.pk) is None
    session.refresh_from_db()
    assert session.title == "canopy-web-api-7716"


def test_a_session_with_no_runner_still_autotitles():
    """Web-only sessions (the dev stub, a chat no runner ever picks up) have no
    emdash name to defer to."""
    user = User.objects.create_user("t2", "t2@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w-title2", display_name="W", created_by=user)
    session = Session.objects.create(workspace=ws, created_by=user, title="")
    Message.objects.create(session=session, turn_index=0, role=Message.USER,
                           plaintext="hello there")
    assert autotitle.maybe_autotitle(session.pk) == "hello there"
