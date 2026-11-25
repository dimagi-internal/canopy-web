"""Sources and their participants: filled only from what canopy already knows; sealed the way the
session ACL seals; and the `CoachingOptIn` copy keeps every live opt-in."""
from __future__ import annotations

import importlib
import io

import pytest
from django.apps import apps
from django.core.management import call_command
from django.utils import timezone

from apps.agents.models import Agent
from apps.brain import participants, sources
from apps.brain.models import BrainSource, LearningConsent, SourceParticipant
from apps.canopy_sessions import access
from apps.canopy_sessions.models import CoachingOptIn, RunnerBinding, Session
from apps.contacts.models import Contact
from apps.harness import runner_requirements as rr
from apps.harness.models import Runner
from tests.fixtures.brain_world import brain_world, user

pytestmark = pytest.mark.django_db
copy = importlib.import_module("apps.brain.migrations.0002_copy_coaching_opt_ins").copy


@pytest.fixture()
def w():
    return brain_world()


def who(src):
    return {(p.user.username, p.how) for p in src.participants.select_related("user")}


def test_participants_come_from_the_session_s_own_rows(w):
    assert who(w["source"]) == {("alice", "creator"), ("bob", "participant")}
    assert w["source"].workspace_id == "acme" and w["source"].ref == str(w["session"].pk)


def test_refresh_follows_the_session_and_never_reopens_a_withdrawal(w):
    from apps.canopy_sessions.models import SessionParticipant

    SessionParticipant.objects.filter(session=w["session"], user=w["bob"]).delete()
    SessionParticipant.objects.create(session=w["session"], user=w["carol"])
    BrainSource.objects.filter(pk=w["source"].pk).update(state=BrainSource.WITHDRAWN)
    src = sources.for_session(w["session"])
    assert who(src) == {("alice", "creator"), ("carol", "participant")}
    assert src.state == BrainSource.WITHDRAWN
    assert BrainSource.objects.count() == 1


def test_a_runner_session_belongs_to_the_box_owner(w):
    box = Runner.objects.create(name="r", kind=Runner.EMDASH, host="h", owner=w["carol"])
    s = Session.objects.create(workspace=w["ws"], origin=Session.ORIGIN_RUNNER)
    RunnerBinding.objects.create(session=s, runner=box)
    assert who(sources.for_session(s)) == {("carol", "box_owner")}


def test_a_contacts_conversation_has_the_contact_and_is_sealed(w):
    c = Contact.objects.create(workspace=w["ws"], email="z@example.org")
    s = Session.objects.create(workspace=w["ws"], contact=c, origin=Session.ORIGIN_WEB)
    src = sources.for_session(s)
    assert src.participants.get().contact_id == c.pk and src.sealed_reason == "contact"


def test_agents_are_never_participants(w):
    bot = Agent.objects.create(slug="hal", name="Hal", owner=w["owner"], workspace=w["ws"], user=user("hal_login"))
    s = Session.objects.create(workspace=w["ws"], created_by=bot.user, origin=Session.ORIGIN_WEB, agent=bot)
    assert sources.for_session(s).participants.count() == 0


def test_the_sensitive_test_is_the_session_acl_s(w):
    """`participants.sealed_reason` and `access._not_sensitive_q` read the same key: any value at all."""
    for meta in ({}, {rr.METADATA_KEY: ["sensitive-data"]}, {rr.METADATA_KEY: "odd"}, {rr.METADATA_KEY: []}):
        s = Session.objects.create(workspace=w["ws"], created_by=w["alice"], origin=Session.ORIGIN_WEB, metadata=meta)
        in_acl = Session.objects.filter(pk=s.pk).filter(access._not_sensitive_q()).exists()
        assert (participants.sealed_reason(s) == "") == in_acl, meta


def test_sealed_reasons(w):
    s = w["session"]
    assert participants.sealed_reason(s) == ""
    s.content_deleted_at = timezone.now()
    assert participants.sealed_reason(s) == "words_deleted"
    assert participants.sealed_reason(None) == "words_deleted"                # no session: shut


def test_the_backfill_makes_a_source_for_every_session_and_can_run_twice(w):
    for _ in range(3):
        Session.objects.create(workspace=w["ws"], created_by=w["carol"], origin=Session.ORIGIN_WEB)
    out = io.StringIO()
    call_command("brain_backfill", batch=2, stdout=out)
    call_command("brain_backfill", batch=2, stdout=out)
    assert BrainSource.objects.count() == Session.objects.count() == 4
    assert SourceParticipant.objects.filter(source__ref=str(w["session"].pk)).count() == 2


# ---- the CoachingOptIn copy (0002) ---------------------------------------------------------------

def test_live_opt_ins_become_individual_consents_and_the_old_rows_stay(w):
    CoachingOptIn.objects.create(user=w["alice"])
    CoachingOptIn.objects.create(user=w["bob"], revoked_at=timezone.now())     # opted out already
    copy(apps, None)
    copy(apps, None)                                                           # idempotent
    rows = LearningConsent.objects.all()
    assert [(r.user.username, r.classes, r.revoked_at) for r in rows] == [("alice", ["individual"], None)]
    assert rows[0].created_at == CoachingOptIn.objects.get(user=w["alice"]).created_at
    assert CoachingOptIn.objects.count() == 2                                  # additive: nothing dropped


def _brain_run_conversation(w):
    """What `runs.start` makes: the operator agent's conversation stamped `brain_run`, its turn canopy's own but
    accountable to the admin who registered the feature (`initiator_user`). Its assistant lines summarise
    other people's sessions."""
    import uuid

    from apps.canopy_sessions.models import Message
    from apps.harness.models import Turn

    bot = Agent.objects.create(slug="learner", name="Learner", owner=w["owner"], workspace=w["ws"],
                               user=user("learner_login"))
    talk = Session.objects.create(workspace=w["ws"], origin=Session.ORIGIN_WEB, agent=bot, metadata={"brain_run": "7"})
    Turn.objects.create(chat_session=talk, origin=Turn.ORIGIN_API, prompt="/learn",
                        idempotency_key=uuid.uuid4().hex, initiator_user=w["owner"])
    Message.objects.create(session=talk, role=Message.ASSISTANT, turn_index=0, plaintext="bob said: the plan")
    return talk


def test_a_brain_run_s_own_conversation_is_never_a_learning_source(w):
    from apps.brain import associate
    from apps.brain.eligibility import eligible, eligible_sources

    talk = _brain_run_conversation(w)
    LearningConsent.objects.create(user=w["owner"], classes=["individual", "memory", "project", "system"])
    associate.run_due()
    assert not BrainSource.objects.filter(ref=str(talk.pk)).exists()             # the associator makes none
    assert participants.sealed_reason(talk) == "brain_run"
    src = sources.for_session(talk)                                               # one made some other way is sealed
    assert src.sealed_reason == "brain_run"
    for cap, purpose in ((w["coach"], "coach"), (w["mem"], "remember")):
        assert not eligible(src, cap, purpose, w["owner"])
        assert not eligible_sources(cap, purpose, w["owner"]).filter(pk=src.pk).exists()
    for cap in (w["proj"], w["sys"]):
        assert not eligible(src, cap, "learn")
        assert not eligible_sources(cap, "learn").filter(pk=src.pk).exists()
