"""A mail thread's WORK session is the row a person looks for, so it has to list.

Each email thread has an anchor (no binding, no transcript: `harness.mail_anchor`,
never listed) and a work session the runner runs in (binding, transcript). Before
this the work session had no link to its turns: an email turn has a `chat_session`
(the anchor) and no `Turn.agent`, so nothing joined it. And a reply on a closed
thread ran in the closed work session, which stayed archived.

Session 072bea9c (ace, 2026-11-04): closed at 04:48, then the replies at 04:50 and
07:39 ran in it while it stayed off the list; only the anchor was revived.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import control, feed, lifecycle
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.canopy_sessions.staleness import active, open_reason
from apps.events.models import Event
from apps.harness import mail_anchor, session_sync
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

THREAD = "1a1a1ea35aa5be78"


@pytest.fixture
def ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws)
    cloud = Runner.objects.create(
        name="cloud-ec2-live", kind=Runner.CLOUD, workspace=ws, location=Runner.LOCAL,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), owner=user, host="cloud-ec2-2",
        capabilities={"sessions": True})
    anchor = session_sync.email_thread_session(agent, THREAD, "Feedback on RUTF supply dashboard")

    class Ctx:
        pass

    c = Ctx()
    c.user, c.agent, c.cloud, c.anchor = user, agent, cloud, anchor
    return c


def _mail_turn(ctx, name, *, thread=THREAD, mode="auto", kind="user", user=None):
    return Turn.objects.create(
        chat_session=ctx.anchor, origin=Turn.ORIGIN_EMAIL, idempotency_key=name,
        status=Turn.RUNNING, turn_mode=mode, claimed_by=ctx.cloud,
        initiator_kind=kind, initiator_user=ctx.user if user is None else user,
        origin_ref={"thread_id": thread})


def _record(ctx, turn=None, *, thread=THREAD, key="05c619da"):
    return session_sync.record_session(
        ctx.agent, thread, runner=ctx.cloud, session_key=key, title="Feedback on RUTF", turn=turn,
    ).session


def _finish(turn, *, key=None, when=None):
    """`key`: what the runner reports at finish. A first cloud turn reports `cloud-<id>`,
    not the binding's key (turn 62bc6a85), so a join on the key would miss it."""
    when = when or timezone.now()
    Turn.objects.filter(pk=turn.pk).update(
        status=Turn.DONE, finished_at=when, created_at=when,
        session_key=key if key is not None else f"cloud-{str(turn.pk)[:8]}")


def _listed():
    return dict(active(Session.objects.all()).annotate(r=open_reason()).values_list("pk", "r"))


def test_a_persons_auto_mail_lists_its_work_session_and_never_its_anchor(ctx):
    turn = _mail_turn(ctx, "m1")
    work = _record(ctx, turn)
    _finish(turn)
    assert _listed() == {work.pk: "person_started"}


def test_the_anchor_turn_links_to_its_work_session(ctx):
    turn = _mail_turn(ctx, "m1")
    work = _record(ctx, turn)
    turn.refresh_from_db()
    assert turn.canopy_session_id == work.pk
    assert turn.chat_session_id == ctx.anchor.pk


def test_a_work_session_shows_as_running_while_its_mail_turn_runs(ctx):
    turn = _mail_turn(ctx, "m1")
    work = _record(ctx, turn)
    assert _listed() == {work.pk: "turn_running"}


def test_a_mail_conversation_idle_for_a_day_retires(ctx):
    turn = _mail_turn(ctx, "m1")
    _record(ctx, turn)
    _finish(turn, when=timezone.now() - dt.timedelta(days=2))
    assert _listed() == {}


def test_a_reply_on_a_closed_thread_reopens_the_work_session_it_runs_in(ctx):
    first = _mail_turn(ctx, "m1")
    work = _record(ctx, first)
    _finish(first)
    assert control.close_session(session=work) == "closed"
    assert _listed() == {}

    reply = _mail_turn(ctx, "m2")
    assert _record(ctx, reply).pk == work.pk
    work.refresh_from_db()
    assert work.status == Session.ACTIVE
    assert _listed() == {work.pk: "turn_running"}
    _finish(reply, key="05c619da")
    assert _listed() == {work.pk: "person_started"}
    # The anchor follows its work session: open while it is.
    assert mail_anchor.why_not(ctx.anchor) == f"its work session {work.pk} is still open"
    event = (Event.objects.filter(kind=lifecycle.UNARCHIVED, payload__session=str(work.pk))
             .order_by("-id").first())
    assert event is not None and event.payload["reason"] == "new mail on the thread"


def test_an_older_web_chat_does_not_hide_a_new_reply(ctx):
    """072bea9c's first LINKED turn was a chat from the day before. Unlinked mail did
    not count as activity, so its idle clock ran from that chat alone."""
    work = _record(ctx, _mail_turn(ctx, "m1"))
    chat = Turn.objects.create(chat_session=work, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               idempotency_key="c1", status=Turn.DONE, turn_mode="auto",
                               claimed_by=ctx.cloud, initiator_kind="user", initiator_user=ctx.user)
    two_days_ago = timezone.now() - dt.timedelta(days=2)
    Turn.objects.filter(pk=chat.pk).update(created_at=two_days_ago, finished_at=two_days_ago)
    Turn.objects.filter(idempotency_key="m1").update(
        status=Turn.DONE, created_at=two_days_ago, finished_at=two_days_ago)
    assert _listed() == {}

    reply = _mail_turn(ctx, "m2")
    _record(ctx, reply)
    _finish(reply)
    assert _listed() == {work.pk: "person_started"}


@pytest.mark.parametrize("thread_key", [THREAD, f"{THREAD}#reader"])
def test_a_runner_that_names_no_turn_still_links_and_reopens(ctx, thread_key):
    """The laptop runner sends no `turn_id`; a confined turn's key carries `#<capability>`."""
    first = _mail_turn(ctx, "m1")
    work = _record(ctx, thread=thread_key)
    first.refresh_from_db()
    assert first.canopy_session_id == work.pk
    _finish(first)
    Session.objects.filter(pk=work.pk).update(status=Session.ARCHIVED)

    reply = _mail_turn(ctx, "m2")
    _record(ctx, thread=thread_key)
    work.refresh_from_db()
    reply.refresh_from_db()
    assert work.status == Session.ACTIVE and reply.canopy_session_id == work.pk


def test_a_record_with_no_mail_running_leaves_a_closed_session_closed(ctx):
    """A retried record-session after the turn ended, or a turn on another thread, must
    not undo a person's close."""
    first = _mail_turn(ctx, "m1")
    work = _record(ctx, first)
    _finish(first)
    assert control.close_session(session=work) == "closed"

    _mail_turn(ctx, "other", thread="another-thread")
    _record(ctx)
    work.refresh_from_db()
    assert work.status == Session.ARCHIVED


@pytest.mark.parametrize("starter", ["trigger", "contact", "unverified", "agent_login"])
def test_mail_no_member_is_following_still_retires_when_it_finishes(ctx, starter):
    """An alarm trigger's turn (canopy itself), an outside sender's mail or another
    agent's mail is not a member's conversation: nothing in flight, nothing to list."""
    if starter == "trigger":
        turn = _mail_turn(ctx, "m1", kind="system")
    elif starter == "contact":
        turn = _mail_turn(ctx, "m1", kind="contact")
    elif starter == "unverified":
        turn = _mail_turn(ctx, "m1", kind="unknown")
    else:
        login = User.objects.create_user("eva-login", "eva@dimagi-ai.com", "pw")
        Agent.objects.create(slug="eva", name="Eva", workspace=ctx.agent.workspace, user=login)
        turn = _mail_turn(ctx, "m1", user=login)
    work = _record(ctx, turn)
    assert _listed() == {work.pk: "turn_running"}
    _finish(turn)
    assert _listed() == {}


def test_a_manual_mail_turn_keeps_the_session_open_as_before(ctx):
    turn = _mail_turn(ctx, "m1", mode="manual")
    work = _record(ctx, turn)
    _finish(turn, when=timezone.now() - dt.timedelta(days=2))
    assert _listed() == {work.pk: "manual_turn"}


def test_the_feed_still_holds_auto_mail_back(ctx):
    """Listing is not notifying: a person's auto mail got no 'done' push before this, and
    the reply reaches them by mail."""
    turn = _mail_turn(ctx, "m1")
    work = _record(ctx, turn)
    assert feed.turn_ran_on_its_own(ctx.anchor, turn) is True
    assert feed.ran_on_its_own(Session.objects.get(pk=work.pk)) is True


def test_a_session_with_a_binding_elsewhere_keeps_its_own_turns(ctx):
    """First write wins: a later record for the same turn never moves it."""
    turn = _mail_turn(ctx, "m1")
    work = _record(ctx, turn)
    other = _record(ctx, turn, thread="another-thread", key="other")
    assert other.pk != work.pk
    turn.refresh_from_db()
    assert turn.canopy_session_id == work.pk
    assert RunnerBinding.objects.filter(session=work, thread_key=THREAD).exists()
