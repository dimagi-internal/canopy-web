"""One turn per mail thread at a time, for mail that fires an inbound_mail trigger.

Thread 1a184f500cd30f72 (2026-10-29): one CloudWatch incident enqueued six full Hal
turns in twelve hours as the thread grew to 14 messages (1, 5, 6, 8, 11, 14).
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import caller_context, mail_coalesce, services
from apps.harness.models import AgentSchedule, PendingMail, Turn

pytestmark = pytest.mark.django_db

SNS = "no-reply@sns.amazonaws.com"
TID = "1a184f500cd30f72"
_DKIM = [{"name": "Authentication-Results",
          "value": "mx.google.com; dkim=pass header.i=@sns.amazonaws.com header.d=sns.amazonaws.com; "
                   "spf=pass smtp.mailfrom=bounce.sns.amazonaws.com"}]


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user(username="op", email="op@dimagi.com", password="x")


@pytest.fixture
def agent(owner):
    from apps.workspaces.models import Workspace, WorkspaceMembership
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner)


@pytest.fixture
def trigger(agent, owner):
    return AgentSchedule.objects.create(
        agent=agent, kind=AgentSchedule.INBOUND_MAIL, name="AWS CloudWatch alarms",
        sender_address=SNS, subject_pattern=r'^(ALARM|OK): "', turn_mode="manual",
        created_by=owner, updated_by=owner)


def _mail(agent, count, *, thread=TID, sender=SNS, subject='ALARM: "labs-jj-alb-latency-high"'):
    """What the runner posts for the newest message of a thread `count` long."""
    turn, created = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL,
        idempotency_key=f"email-hal-{thread}-{count}",
        prompt=f"/hal:turn --thread {thread}",
        origin_ref={"from": sender, "thread_id": thread, "subject": subject, "headers": _DKIM,
                    "message_id": f"m{count}", "message_count": count, "discovered_by": "push"},
    )
    return turn, created


def _set(turn, status, *, finished_ago=None):
    fields = {"status": status}
    if status != Turn.QUEUED:
        fields["started_at"] = timezone.now() - timedelta(minutes=60)
    if finished_ago is not None:
        fields["finished_at"] = timezone.now() - finished_ago
    Turn.objects.filter(pk=turn.pk).update(**fields)
    turn.refresh_from_db()
    return turn


def test_first_message_of_a_thread_is_immediate(agent, trigger):
    turn, created = _mail(agent, 1)
    assert created and turn.status == Turn.QUEUED
    assert "coalesced_messages" not in turn.origin_ref


def test_mail_while_queued_folds_into_that_turn(agent, trigger):
    first, _ = _mail(agent, 1)
    second, created = _mail(agent, 5)
    third, _ = _mail(agent, 6)
    assert not created and second.pk == first.pk == third.pk
    first.refresh_from_db()
    assert first.origin_ref["message_id"] == "m6"
    assert first.origin_ref["message_count"] == 6
    assert first.origin_ref["coalesced_messages"] == 2
    assert Turn.objects.filter(chat_session=first.chat_session).count() == 1
    assert caller_context._trigger(first, first.origin_ref)["coalesced_messages"] == 2


def test_the_same_message_twice_is_counted_once(agent, trigger):
    """Push and poll (or two runners) post one message: one fold, not two."""
    first, _ = _mail(agent, 1)
    _mail(agent, 5)
    again, created = _mail(agent, 5)
    assert not created and again.pk == first.pk
    first.refresh_from_db()
    assert first.origin_ref["coalesced_messages"] == 1


def test_mail_while_running_queues_one_follow_up(agent, trigger):
    first, _ = _mail(agent, 1)
    _set(first, Turn.RUNNING)
    follow, created = _mail(agent, 5)
    assert created and follow.pk != first.pk and follow.status == Turn.QUEUED
    later, created = _mail(agent, 6)
    assert not created and later.pk == follow.pk
    assert Turn.objects.filter(chat_session=first.chat_session).count() == 2


def test_mail_inside_the_quiet_window_is_held_then_released_as_one_turn(agent, trigger):
    first, _ = _mail(agent, 1)
    _set(first, Turn.DONE, finished_ago=timedelta(minutes=5))
    answer, created = _mail(agent, 5)
    assert not created and answer.pk == first.pk
    _mail(agent, 6)
    _mail(agent, 8)
    held = PendingMail.objects.get(session=first.chat_session)
    assert held.count == 3
    assert Turn.objects.filter(chat_session=first.chat_session).count() == 1
    assert mail_coalesce.release_due() == 0, "not due yet"
    assert mail_coalesce.release_due(now=held.due_at + timedelta(seconds=1)) == 1
    follow = Turn.objects.filter(chat_session=first.chat_session).exclude(pk=first.pk).get()
    assert follow.status == Turn.QUEUED
    assert follow.origin_ref["message_count"] == 8
    assert follow.origin_ref["coalesced_messages"] == 2
    assert follow.origin_ref["trigger_id"] == trigger.pk
    assert not PendingMail.objects.exists()
    # A re-post of a message it carries is no new turn.
    again, created = _mail(agent, 6)
    assert not created and again.pk == follow.pk


def test_the_window_is_measured_from_the_finish_not_the_last_message(agent, trigger):
    first, _ = _mail(agent, 1)
    _set(first, Turn.DONE, finished_ago=timedelta(minutes=5))
    _mail(agent, 5)
    held = PendingMail.objects.get()
    assert abs((held.due_at - (first.finished_at + timedelta(minutes=30))).total_seconds()) < 1


def test_mail_after_the_window_starts_a_turn_now(agent, trigger):
    first, _ = _mail(agent, 1)
    _set(first, Turn.DONE, finished_ago=timedelta(minutes=31))
    turn, created = _mail(agent, 5)
    assert created and turn.status == Turn.QUEUED


def test_a_zero_window_is_immediate(agent, trigger):
    AgentSchedule.objects.filter(pk=trigger.pk).update(mail_quiet_minutes=0)
    first, _ = _mail(agent, 1)
    _set(first, Turn.DONE, finished_ago=timedelta(minutes=1))
    turn, created = _mail(agent, 5)
    assert created


def test_a_refused_turn_does_not_start_a_window(agent, trigger):
    """Only a turn that RAN holds the thread quiet."""
    first, _ = _mail(agent, 1)
    Turn.objects.filter(pk=first.pk).update(status=Turn.CANCELLED, finished_at=timezone.now())
    turn, created = _mail(agent, 5)
    assert created


def test_a_running_turn_supersedes_what_is_held(agent, trigger):
    """Held mail, then a turn starts on the thread another way: the follow-up
    queued behind it carries the held messages."""
    first, _ = _mail(agent, 1)
    _set(first, Turn.DONE, finished_ago=timedelta(minutes=5))
    _mail(agent, 5)
    _set(first, Turn.RUNNING)   # e.g. someone re-ran it
    follow, created = _mail(agent, 6)
    assert created
    assert follow.origin_ref["coalesced_messages"] == 1
    assert not PendingMail.objects.exists()


def test_release_waits_while_the_thread_is_busy(agent, trigger):
    first, _ = _mail(agent, 1)
    _set(first, Turn.DONE, finished_ago=timedelta(minutes=5))
    _mail(agent, 5)
    held = PendingMail.objects.get()
    _set(first, Turn.RUNNING)
    assert mail_coalesce.release_due(now=held.due_at + timedelta(seconds=1)) == 0
    assert PendingMail.objects.exists()


def test_mail_from_a_person_is_never_coalesced(agent, trigger, owner):
    """No trigger fired: a person writing twice is two asks (each still answered,
    one after the other)."""
    a, _ = _mail(agent, 1, thread="p1", sender="stranger@example.com", subject="hi")
    b, created = _mail(agent, 2, thread="p1", sender="stranger@example.com", subject="hi")
    assert created and a.pk != b.pk


def test_separate_threads_are_separate(agent, trigger):
    a, _ = _mail(agent, 1, thread="A")
    b, created = _mail(agent, 1, thread="B")
    assert created and a.pk != b.pk


def test_the_quiet_window_is_a_trigger_setting(agent, owner):
    from apps.harness import schedule_services
    t = schedule_services.create_schedule(owner, "hal", {
        "kind": "inbound_mail", "name": "alarms", "sender_address": SNS,
        "mail_quiet_minutes": 45})
    assert t.mail_quiet_minutes == 45
    assert schedule_services.serialize_trigger(t)["mail_quiet_minutes"] == 45


def test_held_mail_keeps_the_thread_anchor_open(agent, trigger):
    from apps.harness import mail_anchor
    first, _ = _mail(agent, 1)
    _set(first, Turn.DONE, finished_ago=timedelta(minutes=5))
    _mail(agent, 5)
    assert "held" in mail_anchor.why_not(first.chat_session)
