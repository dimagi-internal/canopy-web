"""Content retention (apps/retention): which rule wins, what a purge drops and
keeps, that purged chat history cannot be written back by a runner re-ship,
and that nothing happens until it is switched on.

Spec: docs/superpowers/specs/2026-10-05-content-retention-design.md.
"""
from __future__ import annotations

import datetime as dt
import gzip
import uuid

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import services as chat_services
from apps.canopy_sessions.models import Draft, Message, RunnerBinding, Session
from apps.harness.models import Turn, TurnEvent, TurnTranscript
from apps.retention import policy, services
from apps.retention.models import RetentionRule, RetentionSweep
from apps.retention.policy import Policy, Subject
from apps.session_sharing.models import Session as SharedSession
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

NOW = timezone.now()


def days_ago(n: float) -> dt.datetime:
    return NOW - dt.timedelta(days=n)


@pytest.fixture
def world():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=user)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=user,
                                       parent=dimagi)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=connect, owner=user)
    ace = Agent.objects.create(slug="ace", name="Ace", workspace=connect, owner=user)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=dimagi, owner=user)
    return type("W", (), dict(user=user, dimagi=dimagi, connect=connect, hal=hal, ace=ace, eva=eva))


def rule(**kw) -> RetentionRule:
    return RetentionRule.objects.create(**kw)


def a_turn(agent, *, origin=Turn.ORIGIN_EMAIL, status=Turn.DONE, age_days=0, initiator="user",
           chat_session=None, prompt="the email body"):
    turn = Turn.objects.create(
        agent=None if chat_session else agent, chat_session=chat_session, origin=origin,
        status=status, prompt=prompt, result_note="did it", report_summary="summary",
        report_title="Replied to Beth", initiator_kind=initiator,
        idempotency_key=uuid.uuid4().hex,
    )
    TurnEvent.objects.create(turn=turn, seq=1, kind="assistant", payload={"text": "secret reply"})
    TurnTranscript.objects.create(turn=turn, raw_jsonl_gz=gzip.compress(b'{"a":1}\n'), line_count=1)
    Turn.objects.filter(pk=turn.pk).update(created_at=days_ago(age_days), finished_at=days_ago(age_days))
    return turn


def a_chat(world, *, agent=None, age_days=0, n_messages=4, **kw):
    session = Session.objects.create(workspace=world.connect, agent=agent or world.hal,
                                     created_by=world.user, title="About the deploy", **kw)
    for i in range(n_messages):
        Message.objects.create(session=session, turn_index=i * 10, role=Message.ASSISTANT,
                               plaintext=f"line {i}", content={})
    Session.objects.filter(pk=session.pk).update(created_at=days_ago(age_days), updated_at=days_ago(age_days))
    Message.objects.filter(session=session).update(created_at=days_ago(age_days))
    return session


# --------------------------------------------------------------- resolution

def test_no_rule_means_keep_forever(world):
    assert Policy.load().resolve(Subject(kind="turn", workspace="connect")) is None


def test_the_worked_example_from_the_spec(world):
    rule(kind=RetentionRule.TURN, keep_days=90)
    rule(workspace=world.dimagi, principal=RetentionRule.CONTACT, keep_days=7)
    chat30 = rule(workspace=world.connect, kind=RetentionRule.CHAT, keep_days=30)
    hal_forever = rule(workspace=world.connect, kind=RetentionRule.CHAT, agent=world.hal, keep_days=None)
    p = Policy.load()

    # A contact chatting with ace in connect: connect has a matching rule, so
    # dimagi's contact rule is never consulted.
    assert p.resolve(Subject(kind="chat", workspace="connect", principal="contact",
                             agent_id=world.ace.pk)) == chat30
    # A member's chat with hal: the rule naming the agent is more specific.
    assert p.resolve(Subject(kind="chat", workspace="connect", principal="member",
                             agent_id=world.hal.pk)) == hal_forever
    # An email turn for eva in dimagi: dimagi's only rule is about contacts, so
    # it falls through to the deployment-wide rule.
    got = p.resolve(Subject(kind="turn", workspace="dimagi", source="email", principal="member",
                            agent_id=world.eva.pk))
    assert got.keep_days == 90 and got.workspace_id is None


def test_a_child_inherits_its_parents_rule(world):
    parent = rule(workspace=world.dimagi, keep_days=14)
    assert Policy.load().resolve(Subject(kind="turn", workspace="connect")) == parent


def test_equally_specific_rules_tie_to_the_shorter(world):
    rule(workspace=world.connect, source="email", keep_days=60)
    short = rule(workspace=world.connect, principal="contact", keep_days=5)
    got = Policy.load().resolve(Subject(kind="turn", workspace="connect", source="email",
                                        principal="contact"))
    assert got == short


def test_keep_forever_beats_a_broader_rule(world):
    rule(workspace=world.connect, keep_days=7)
    exempt = rule(workspace=world.connect, source="canopy_scheduler", keep_days=None)
    assert Policy.load().resolve(Subject(kind="turn", workspace="connect",
                                         source="canopy_scheduler")) == exempt


def test_rule_validation(world):
    rule(workspace=world.connect, kind="chat", keep_days=30)
    with pytest.raises(ValidationError):
        rule(workspace=world.connect, kind="chat", keep_days=7)  # same filters
    with pytest.raises(ValidationError):
        rule(kind="turn", keep_days=0)
    with pytest.raises(ValidationError):
        rule(workspace=world.connect, kind="shared", keep_days=7)
    with pytest.raises(ValidationError):
        rule(workspace=world.dimagi, agent=world.hal, keep_days=7)  # hal lives in connect


def test_vocabularies_match_their_definitions():
    from apps.harness.services import EMAIL_THREAD_KEY
    from apps.slack.services import SLACK_THREAD_KEY

    origins = {value for value, _ in Turn.ORIGIN_CHOICES}
    sources = {value for value, _ in RetentionRule.SOURCE_CHOICES}
    assert sources == origins | {RetentionRule.EMDASH}
    assert policy.SLACK_THREAD_KEY == SLACK_THREAD_KEY
    assert policy.EMAIL_THREAD_KEY == EMAIL_THREAD_KEY


def test_how_a_chat_is_described():
    assert policy.chat_source(metadata={"source": "ace-web"}, origin="web") == "ace_web"
    assert policy.chat_source(metadata={"source": "email"}, origin="runner") == "email"
    assert policy.chat_source(metadata={"slack_thread": "T:C:1"}, origin="web") == "slack"
    assert policy.chat_source(metadata={}, origin="runner") == "emdash"
    assert policy.chat_source(metadata={}, origin="web") == "canopy_web_chat"
    assert policy.chat_principal(contact_id=3, created_by_id=None) == "contact"
    assert policy.chat_principal(contact_id=None, created_by_id=1) == "member"
    assert policy.chat_principal(contact_id=None, created_by_id=None) == "system"
    assert policy.turn_principal("unknown") == "system"


# -------------------------------------------------------------------- turns

def test_dry_run_counts_and_changes_nothing(world):
    rule(kind="turn", keep_days=30)
    old = a_turn(world.hal, age_days=40)
    record = services.sweep(apply=False, now=NOW)
    assert record.counts["totals"] == {"turns_scrubbed": 1}
    old.refresh_from_db()
    assert old.prompt == "the email body" and old.content_purged_at is None
    assert TurnEvent.objects.filter(turn=old).exists()


def test_apply_scrubs_turn_content_and_keeps_the_row(world):
    rule(kind="turn", keep_days=30)
    old = a_turn(world.hal, age_days=40)
    young = a_turn(world.hal, age_days=10)
    running = a_turn(world.ace, age_days=40, status=Turn.RUNNING)
    services.sweep(apply=True, now=NOW)

    old.refresh_from_db()
    assert (old.prompt, old.result_note, old.report_summary) == ("", "", "")
    assert old.report_title == "Replied to Beth" and old.status == Turn.DONE  # metadata stays
    assert old.content_purged_at is not None
    assert not TurnEvent.objects.filter(turn=old).exists()
    assert not TurnTranscript.objects.filter(turn=old).exists()
    for kept in (young, running):  # too young; still executing
        kept.refresh_from_db()
        assert kept.prompt == "the email body"


def test_rules_are_granular_by_source_and_principal(world):
    rule(workspace=world.connect, kind="turn", principal="contact", keep_days=3)
    rule(workspace=world.connect, kind="turn", source="canopy_scheduler", keep_days=None)
    contact = a_turn(world.hal, age_days=5, initiator="contact")
    member = a_turn(world.hal, age_days=5, initiator="user")
    services.sweep(apply=True, now=NOW)
    contact.refresh_from_db()
    member.refresh_from_db()
    assert contact.prompt == "" and member.prompt == "the email body"


# -------------------------------------------------------------------- chats

def test_chat_purge_drops_the_expired_prefix_and_its_turns(world):
    rule(workspace=world.connect, kind="chat", keep_days=30)
    session = a_chat(world, age_days=40)
    Message.objects.filter(session=session, turn_index=30).update(created_at=days_ago(1))
    sturn = a_turn(None, chat_session=session, age_days=40, origin=Turn.ORIGIN_CANOPY_WEB_CHAT)
    Draft.objects.create(session=session, author=world.user, body="half a thought")
    Draft.objects.filter(session=session).update(updated_at=days_ago(40))

    record = services.sweep(apply=True, now=NOW)

    assert record.counts["totals"]["chat_messages"] == 3
    assert list(session.messages.values_list("turn_index", flat=True)) == [30]
    session.refresh_from_db()
    assert session.retention_floor_index == 21  # newest expired was 20
    assert session.status == Session.ACTIVE and session.title  # not idle: a recent message
    assert not Draft.objects.filter(session=session).exists()
    sturn.refresh_from_db()
    assert sturn.prompt == "" and sturn.content_purged_at is not None


def test_an_idle_chat_is_emptied_and_archived(world):
    rule(workspace=world.connect, kind="chat", keep_days=30)
    session = a_chat(world, age_days=40)
    RunnerBinding.objects.create(session=session, tail=[{"text": "hi"}], summary="s")
    services.sweep(apply=True, now=NOW)
    session.refresh_from_db()
    assert not session.messages.exists()
    assert (session.title, session.status) == ("", Session.ARCHIVED)
    binding = RunnerBinding.objects.get(session=session)
    assert (binding.tail, binding.summary) == ([], "")


def test_purged_history_cannot_be_written_back_by_a_reship(world):
    rule(workspace=world.connect, kind="chat", keep_days=30)
    session = a_chat(world, age_days=40)  # indices 0, 10, 20, 30
    services.sweep(apply=True, now=NOW)
    assert not session.messages.exists()

    # The runner re-ships the whole transcript (backfill / reset), plus one new row.
    rows = [{"index": i, "role": "assistant", "text": f"line {i}"} for i in (0, 10, 20, 30)]
    rows.append({"index": 40, "role": "assistant", "text": "new"})
    assert chat_services.write_backfill(session, rows) == 1
    assert list(session.messages.values_list("plaintext", flat=True)) == ["new"]


def test_server_assigned_indices_start_at_the_floor(world):
    rule(workspace=world.connect, kind="chat", keep_days=30)
    session = a_chat(world, age_days=40)
    services.sweep(apply=True, now=NOW)
    session.refresh_from_db()
    assert chat_services._next_index(session) == session.retention_floor_index
    # An ordinal-less (legacy) backfill would renumber old history from the floor up.
    assert chat_services.write_backfill(session, [{"role": "assistant", "text": "old"}]) == 0


def test_a_kept_forever_agent_is_untouched(world):
    rule(workspace=world.connect, kind="chat", keep_days=30)
    rule(workspace=world.connect, kind="chat", agent=world.hal, keep_days=None)
    session = a_chat(world, age_days=400)
    services.sweep(apply=True, now=NOW)
    assert session.messages.count() == 4


# ------------------------------------------------------- shared transcripts

def test_shared_transcripts_are_deleted(world):
    rule(kind="shared", keep_days=30)
    old = SharedSession.objects.create(owner=world.user, title="old")
    SharedSession.objects.filter(pk=old.pk).update(created_at=days_ago(31))
    new = SharedSession.objects.create(owner=world.user, title="new")
    services.sweep(apply=True, now=NOW)
    assert list(SharedSession.objects.values_list("pk", flat=True)) == [new.pk]


# ---------------------------------------------------------------- switching

def test_off_by_default_even_with_rules(world, settings):
    assert settings.CANOPY_RETENTION_ENFORCE is False
    rule(kind="turn", keep_days=1)
    old = a_turn(world.hal, age_days=40)
    assert services.maybe_sweep(NOW) is None
    old.refresh_from_db()
    assert old.prompt and not RetentionSweep.objects.exists()


def test_when_enforced_the_heartbeat_sweeps_at_most_hourly(world, settings):
    settings.CANOPY_RETENTION_ENFORCE = True
    cache.delete(services._LOCK_KEY)
    rule(kind="turn", keep_days=1)
    old = a_turn(world.hal, age_days=40)
    first = services.maybe_sweep(NOW)
    assert first is not None and first.applied and first.trigger == "heartbeat"
    old.refresh_from_db()
    assert old.prompt == ""
    assert services.maybe_sweep(NOW) is None  # locked for the hour
    cache.delete(services._LOCK_KEY)


def test_the_command_dry_runs_by_default(world, capsys):
    from django.core.management import call_command

    rule(kind="turn", keep_days=30)
    old = a_turn(world.hal, age_days=40)
    call_command("retention_sweep")
    assert "dry run" in capsys.readouterr().out
    old.refresh_from_db()
    assert old.prompt
