"""The "your chat is done" pushes.

Default: one push once a session has stayed quiet for the recipient's
`session_idle_minutes` (default 5) after a turn ends — drained by the runner
heartbeat, cancelled by a new turn. A session flagged `notify_every_completion`
pushes immediately on every finished turn instead. Either way the tap lands on
the chat."""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Message, Session
from apps.harness import services as hsvc
from apps.harness.models import Runner, Turn
from apps.push import services as push
from apps.push.models import NotificationPreference, PushSubscription
from apps.workspaces.models import Workspace, WorkspaceMembership

# on_commit carries the every-completion push; it only fires outside the
# per-test atomic wrapper.
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _vapid_configured(settings):
    settings.VAPID_PRIVATE_KEY = "test-vapid-private-key"
    settings.VAPID_SUBJECT = "mailto:test@example.com"


@pytest.fixture()
def user():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    PushSubscription.objects.create(
        user=u, endpoint="https://fcm.googleapis.com/fcm/send/AAA", p256dh="k", auth="a"
    )
    return u


@pytest.fixture()
def workspace(user):
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def session(workspace, user):
    return chat.create_session(
        workspace=workspace, created_by=user, project="canopy-web", title="Fix the login bug"
    )


def _finish(session, user, status=Turn.DONE):
    _, turn = chat.send_message(session=session, text="go", user=user)
    # A session id makes this a real attempt: a FAILED turn without one is a
    # non-attempt, which finish_turn requeues instead of failing.
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CLAIMED, emdash_task_id="task-1")
    turn.refresh_from_db()
    return hsvc.finish_turn(turn, status=status)


def _due(session):
    session.refresh_from_db()
    return session.finish_push_due_at


def test_a_finished_turn_arms_a_push_five_minutes_out(session, user):
    before = timezone.now()
    with patch("apps.push.services._send_one") as send:
        _finish(session, user)
    assert send.call_count == 0  # nothing yet — the session might pick straight back up
    due = _due(session)
    assert before + dt.timedelta(minutes=5) <= due <= timezone.now() + dt.timedelta(minutes=5)


def test_the_push_is_sent_once_due_and_lands_on_the_chat(session, user):
    _finish(session, user)
    Message.objects.create(
        session=session, turn_index=99, role=Message.ASSISTANT, plaintext="Fixed it — the cookie was scoped wrong."
    )
    with patch("apps.push.services._send_one") as send:
        assert push.send_due_session_pushes(timezone.now()) == 0  # not due yet
        assert push.send_due_session_pushes(timezone.now() + dt.timedelta(minutes=6)) == 1
        assert push.send_due_session_pushes(timezone.now() + dt.timedelta(minutes=7)) == 0  # once
    payload = send.call_args.args[1]
    assert payload["title"] == "Fix the login bug is done"
    assert payload["body"] == "Fixed it — the cookie was scoped wrong."
    assert payload["url"] == f"/w/canopy/chat/{session.id}"


def test_a_new_turn_cancels_the_pending_push(session, user):
    _finish(session, user)
    assert _due(session) is not None
    chat.send_message(session=session, text="one more thing", user=user)
    assert _due(session) is None


def test_no_push_while_the_session_is_busy_again(session, user):
    _finish(session, user)
    due = _due(session)
    # A turn queued without passing through enqueue (belt and braces).
    Turn.objects.create(
        chat_session=session, origin=Turn.ORIGIN_API, idempotency_key="k2", prompt="x"
    )
    Session.objects.filter(pk=session.pk).update(finish_push_due_at=due)
    with patch("apps.push.services._send_one") as send:
        push.send_due_session_pushes(timezone.now() + dt.timedelta(minutes=6))
    assert send.call_count == 0


def test_the_quiet_window_is_the_users_choice_and_zero_is_off(session, user):
    NotificationPreference.objects.create(user=user, session_idle_minutes=20)
    before = timezone.now()
    _finish(session, user)
    assert _due(session) >= before + dt.timedelta(minutes=20)

    NotificationPreference.objects.filter(user=user).update(session_idle_minutes=0)
    _finish(session, user)
    assert _due(session) is None


def test_every_completion_pushes_immediately_and_arms_nothing(session, user):
    Session.objects.filter(pk=session.pk).update(notify_every_completion=True)
    session.refresh_from_db()
    with patch("apps.push.services._send_one") as send:
        _finish(session, user)
        _finish(session, user)
    assert send.call_count == 2
    assert _due(session) is None


def test_a_failed_turn_says_so(session, user):
    Session.objects.filter(pk=session.pk).update(notify_every_completion=True)
    with patch("apps.push.services._send_one") as send:
        _finish(session, user, status=Turn.FAILED)
    assert send.call_args.args[1]["title"] == "Fix the login bug failed"


def test_a_cancelled_turn_never_pushes(session, user):
    Session.objects.filter(pk=session.pk).update(notify_every_completion=True)
    with patch("apps.push.services._send_one") as send:
        _finish(session, user, status=Turn.CANCELLED)
    assert send.call_count == 0
    assert _due(session) is None


def test_the_runner_heartbeat_drains_due_pushes(session, user, workspace):
    runner = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", paired_by=user, workspace=workspace,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    _finish(session, user)
    Session.objects.filter(pk=session.pk).update(
        finish_push_due_at=timezone.now() - dt.timedelta(seconds=1)
    )
    with patch("apps.push.services._send_one") as send:
        hsvc.heartbeat(runner, active_turn_ids=[])
    assert send.call_count == 1
    assert _due(session) is None


def test_one_drain_is_bounded_so_a_slow_push_cannot_stall_a_heartbeat(workspace, user):
    # The drain runs inside a runner's heartbeat and each send can hang for its
    # 10s timeout, so a backlog is spread over beats rather than sent at once.
    overdue = timezone.now() - dt.timedelta(minutes=1)
    for i in range(push.FINISH_PUSH_BATCH + 3):
        s = chat.create_session(workspace=workspace, created_by=user, project="p", title=f"chat {i}")
        _finish(s, user)
        Session.objects.filter(pk=s.pk).update(finish_push_due_at=overdue)
    with patch("apps.push.services._send_one") as send:
        assert push.send_due_session_pushes(timezone.now()) == push.FINISH_PUSH_BATCH
        assert send.call_count == push.FINISH_PUSH_BATCH
        assert push.send_due_session_pushes(timezone.now()) == 3  # the rest, next beat


def test_preferences_api_defaults_to_five_and_round_trips(user):
    c = Client()
    c.force_login(user)
    assert c.get("/api/push/preferences").json() == {"session_idle_minutes": 5}
    r = c.patch("/api/push/preferences", {"session_idle_minutes": 15}, content_type="application/json")
    assert r.status_code == 200
    assert c.get("/api/push/preferences").json() == {"session_idle_minutes": 15}
    bad = c.patch("/api/push/preferences", {"session_idle_minutes": -1}, content_type="application/json")
    assert bad.status_code == 422


def test_session_notify_toggle_defaults_off_and_round_trips(session, user):
    c = Client()
    c.force_login(user)
    assert c.get(f"/api/canopy-sessions/{session.id}").json()["notify_every_completion"] is False
    r = c.put(
        f"/api/canopy-sessions/{session.id}/notify", {"every_completion": True},
        content_type="application/json",
    )
    assert r.status_code == 200
    assert r.json()["notify_every_completion"] is True
    session.refresh_from_db()
    assert session.notify_every_completion is True
