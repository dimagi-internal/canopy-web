"""A mail thread's anchor session retires on its own once nothing on it is live.

Each inbound email thread gets one anchor Session; each mail is a turn on it, and
the runner runs the work in its OWN session (a RunnerBinding keyed on the Gmail
thread id). The anchor has no binding, so before this nothing ever closed it:
25 alarm anchors sat `active` on 2026-10-29, back to 2026-09-16.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness import mail_anchor, services, session_sync
from apps.harness.closing import close_turn
from apps.harness.models import Runner, Turn

pytestmark = pytest.mark.django_db


SNS = "no-reply@sns.amazonaws.com"
_DKIM = [{"name": "Authentication-Results",
          "value": "mx.google.com; dkim=pass header.i=@sns.amazonaws.com header.d=sns.amazonaws.com; "
                   "spf=pass smtp.mailfrom=bounce.sns.amazonaws.com"}]


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(
        username="owner", email="owner@dimagi.com", password="x")


@pytest.fixture
def workspace(owner):
    from apps.workspaces.models import Workspace, WorkspaceMembership
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture
def agent(workspace, owner):
    """Hal with its CloudWatch trigger, so alarm mail is admitted (a queued turn)."""
    from apps.harness.models import AgentSchedule
    a = Agent.objects.create(slug="hal", name="Hal", workspace=workspace, owner=owner)
    AgentSchedule.objects.create(
        agent=a, kind=AgentSchedule.INBOUND_MAIL, name="AWS CloudWatch alarms",
        sender_address=SNS, subject_pattern=r'^(ALARM|OK): "', turn_mode="manual",
        created_by=owner, updated_by=owner)
    return a


@pytest.fixture
def runner(owner):
    return Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, host="cloud-ec2-1",
                                 owner=owner)


def _mail(agent, thread_id="1a184f500cd30f72", key=None, sender=SNS):
    turn, _ = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL,
        idempotency_key=key or f"email:{uuid.uuid4().hex}",
        prompt=f"/hal:turn --thread {thread_id}",
        origin_ref={"from": sender, "thread_id": thread_id, "headers": _DKIM,
                    "subject": 'ALARM: "labs-jj-alb-latency-high"'},
    )
    return turn


def _run(turn, runner, agent, *, key="c-alarm-0f72", thread_key=None):
    """What a runner does with a mail turn: claim it, run it in its OWN session."""
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING, claimed_by=runner,
                                           claimed_at=timezone.now(), session_key=key)
    work = RunnerBinding.objects.filter(session_key=key, runner=runner).first()
    if work is None:
        s = Session.objects.create(agent=agent, workspace=agent.workspace,
                                   origin=Session.ORIGIN_RUNNER, title=key)
        work = RunnerBinding.objects.create(
            session=s, runner=runner, session_key=key,
            thread_key=thread_key or turn.origin_ref["thread_id"])
    turn.refresh_from_db()
    return work.session


def _finish(turn):
    assert close_turn(turn, Turn.DONE, "done", from_states=[Turn.RUNNING])


def _close_work(work):
    Session.objects.filter(pk=work.pk).update(status=Session.ARCHIVED)
    session_sync.fire_sessions_closed([work.pk])


def _status(session):
    session.refresh_from_db()
    return session.status


def test_anchor_retires_after_turn_done_and_work_session_closed(agent, runner,
                                                                 django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        turn = _mail(agent)
        anchor = turn.chat_session
        work = _run(turn, runner, agent)
        _finish(turn)
    assert _status(anchor) == Session.ACTIVE, "its work session is still open"
    with django_capture_on_commit_callbacks(execute=True):
        _close_work(work)
    assert _status(anchor) == Session.ARCHIVED


def test_work_closed_first_then_turn_finishes(agent, runner, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        turn = _mail(agent)
        anchor = turn.chat_session
        work = _run(turn, runner, agent)
        _close_work(work)
    assert _status(anchor) == Session.ACTIVE, "a running turn keeps it open"
    with django_capture_on_commit_callbacks(execute=True):
        _finish(turn)
    assert _status(anchor) == Session.ARCHIVED


def test_never_retires_while_a_turn_is_queued(agent, runner, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        first = _mail(agent)
        anchor = first.chat_session
        work = _run(first, runner, agent)
        second = _mail(agent)  # queued behind it
        _finish(first)
        _close_work(work)
    assert second.status == Turn.QUEUED
    assert _status(anchor) == Session.ACTIVE
    assert "not finished" in mail_anchor.why_not(anchor)


def test_never_retires_while_work_session_is_live(agent, runner):
    turn = _mail(agent)
    anchor = turn.chat_session
    _run(turn, runner, agent)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE, finished_at=timezone.now())
    assert mail_anchor.sweep() == []
    assert "still open" in mail_anchor.why_not(anchor)


def test_refused_turn_retires_its_anchor(agent, django_capture_on_commit_callbacks):
    """Cancelled at admission ("hal offers nothing to this caller"): no handoff."""
    with django_capture_on_commit_callbacks(execute=True):
        turn = _mail(agent, sender="stranger@example.com")
    assert turn.status == Turn.CANCELLED, "offered nothing: refused at the door"
    assert _status(turn.chat_session) == Session.ARCHIVED


def test_work_session_found_by_thread_key_without_session_key(agent, runner):
    """A cloud turn that never stamped its session_key: joined on the thread id."""
    turn = _mail(agent)
    anchor = turn.chat_session
    work = _run(turn, runner, agent, key="claude-uuid")
    Turn.objects.filter(pk=turn.pk).update(session_key="", status=Turn.DONE,
                                           finished_at=timezone.now())
    assert work.pk in mail_anchor.work_sessions(anchor)
    assert mail_anchor.sweep() == []
    Session.objects.filter(pk=work.pk).update(status=Session.ARCHIVED)
    assert mail_anchor.sweep() == [anchor.pk]


def test_confined_thread_key_is_matched(agent, runner):
    turn = _mail(agent, thread_id="t-conf")
    anchor = turn.chat_session
    work = _run(turn, runner, agent, key="k-conf", thread_key="t-conf#reader")
    Turn.objects.filter(pk=turn.pk).update(session_key="")
    assert work.pk in mail_anchor.work_sessions(anchor)


def test_turnless_anchor_waits_out_its_grace(agent):
    anchor = session_sync.email_thread_session(agent, "lonely", "x")
    assert mail_anchor.sweep() == []
    Session.objects.filter(pk=anchor.pk).update(
        created_at=timezone.now() - mail_anchor.TURNLESS_GRACE - timedelta(seconds=1))
    assert mail_anchor.sweep() == [anchor.pk]


def test_a_bound_session_is_never_an_anchor(agent, runner):
    """A session a runner bound is a work session — its own close governs it."""
    anchor = session_sync.email_thread_session(agent, "bound", "x")
    RunnerBinding.objects.create(session=anchor, runner=runner, session_key="k", thread_key="bound")
    Session.objects.filter(pk=anchor.pk).update(created_at=timezone.now() - timedelta(days=1))
    assert not mail_anchor.maybe_retire(anchor.pk)
    assert _status(anchor) == Session.ACTIVE


def test_a_non_mail_session_is_left_alone(agent):
    s = Session.objects.create(agent=agent, workspace=agent.workspace, origin=Session.ORIGIN_WEB)
    Session.objects.filter(pk=s.pk).update(created_at=timezone.now() - timedelta(days=1))
    assert not mail_anchor.maybe_retire(s.pk)
    assert _status(s) == Session.ACTIVE


def test_new_mail_revives_a_retired_anchor(agent, runner, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        turn = _mail(agent)
        anchor = turn.chat_session
        work = _run(turn, runner, agent)
        _finish(turn)
        _close_work(work)
    assert _status(anchor) == Session.ARCHIVED
    again = _mail(agent)
    assert again.chat_session_id == anchor.pk
    assert _status(anchor) == Session.ACTIVE


def test_backfill_command(agent, runner, capsys):
    turn = _mail(agent)
    anchor = turn.chat_session
    work = _run(turn, runner, agent)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE, finished_at=timezone.now())
    Session.objects.filter(pk=work.pk).update(status=Session.ARCHIVED)
    call_command("retire_mail_anchors", "--dry-run")
    assert "would retire 1" in capsys.readouterr().out
    assert _status(anchor) == Session.ACTIVE
    call_command("retire_mail_anchors")
    assert _status(anchor) == Session.ARCHIVED


def test_one_message_twice_makes_one_anchor(agent):
    """Push and poll discover the same message: one turn, and one session —
    not a turnless sibling (3ee01ce9 beside afe51b3c, 2026-10-29)."""
    a = _mail(agent, thread_id="dup", key="email:same")
    b = _mail(agent, thread_id="dup", key="email:same")
    assert a.pk == b.pk
    assert Session.objects.filter(**{f"metadata__{session_sync.EMAIL_THREAD_KEY}": "email:dup"}).count() == 1
