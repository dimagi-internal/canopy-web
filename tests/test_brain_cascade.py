"""P5: a removal reaches what was derived from it, within one sweep, and stays removed."""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.brain import cascade, consent, sources
from apps.brain.models import (
    BrainOutput,
    BrainSource,
    LearningConsent,
    OutputLineage,
    RederiveRequest,
    RevocationNotice,
    SourceLink,
)
from apps.canopy_sessions.models import Session, SessionParticipant
from tests.fixtures.brain_world import brain_world

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    out = brain_world()
    for u in (out["alice"], out["bob"]):
        LearningConsent.objects.create(user=u, classes=["individual", "project", "memory"])
    return out


def emit(w, cap, subject_type, subject, srcs, scope=None):
    o = BrainOutput.objects.create(capability=w[cap], kind="note", workspace=w["ws"], subject_type=subject_type,
                                   subject=str(subject), title="t", body={"words": "x"})
    for s in srcs:
        OutputLineage.objects.create(output_kind="brain.output", output_id=str(o.pk), source=s,
                                     participant_scope=scope if scope is not None else
                                     list(s.participants.values_list("user_id", flat=True)))
    return o


def reload(o):
    o.refresh_from_db()
    return o


def test_a_persons_removal_withdraws_what_was_learned_about_them_only(w):
    about_alice = emit(w, "mem", "person", w["alice"].pk, [w["source"]])
    about_bob = emit(w, "mem", "person", w["bob"].pk, [w["source"]])
    consent.exclude(w["alice"], w["source"])
    out = cascade.sweep()
    assert out["withdrawn"] == 1
    a, b = reload(about_alice), reload(about_bob)
    assert (a.state, a.body, a.title) == ("withdrawn", {}, "")
    assert (b.state, b.body) == ("active", {"words": "x"})          # bob keeps what came from his own words
    assert RevocationNotice.objects.filter(capability_slug="mem", source=w["source"]).count() == 1


def test_a_shared_output_goes_when_any_participant_removes_the_source(w, django_capture_on_commit_callbacks):
    o = emit(w, "proj", "project", 1, [w["source"]])
    with django_capture_on_commit_callbacks(execute=True):          # prompt: run by the act itself
        consent.exclude(w["bob"], w["source"])
    assert reload(o).state == "withdrawn"


def test_a_mixed_output_is_marked_and_asks_to_be_derived_again(w):
    other = sources.for_session(Session.objects.create(workspace=w["ws"], created_by=w["alice"],
                                                       origin=Session.ORIGIN_WEB))
    o = emit(w, "coach", "person", w["alice"].pk, [w["source"], other])
    consent.exclude(w["alice"], w["source"])
    assert cascade.sweep()["stale"] == 1
    assert reload(o).state == "stale" and (o.body, o.title) == ({}, "")         # its words go, its ids stay
    assert OutputLineage.objects.filter(output_id=str(o.pk)).count() == 2
    assert RederiveRequest.objects.filter(output=o, done_at__isnull=True).count() == 1
    cascade.sweep()                                                   # idempotent
    assert RederiveRequest.objects.filter(output=o).count() == 1 and reload(o).state == "stale"


def test_ending_consent_or_a_class_withdraws_what_it_covered(w):
    o = emit(w, "coach", "person", w["alice"].pk, [w["source"]])
    p = emit(w, "mem", "person", w["alice"].pk, [w["source"]])
    LearningConsent.objects.filter(user=w["alice"]).update(classes=["individual"])
    cascade.sweep()
    assert reload(o).state == "active" and reload(p).state == "withdrawn"
    consent.revoke(w["alice"])
    cascade.sweep()
    assert reload(o).state == "withdrawn"


def test_deleted_words_withdraw_the_source_and_its_outputs_and_scrub_link_evidence(w):
    SourceLink.objects.create(source=w["source"], subject="1", how="signals", tier="core", evidence=["edits x (3)"])
    o = emit(w, "sys", "system", "canopy", [w["source"]])
    Session.objects.filter(pk=w["session"].pk).update(content_deleted_at=timezone.now())
    cascade.sweep()
    assert reload(o).state == "withdrawn"
    w["source"].refresh_from_db()
    assert w["source"].state == BrainSource.WITHDRAWN
    assert SourceLink.objects.get().evidence == []


def test_the_content_deletion_signal_runs_it(w, django_capture_on_commit_callbacks):
    from apps.harness.signals import session_content_deleted

    o = emit(w, "sys", "system", "canopy", [w["source"]])
    Session.objects.filter(pk=w["session"].pk).update(content_deleted_at=timezone.now())
    with django_capture_on_commit_callbacks(execute=True):
        session_content_deleted.send(sender=Session, session=w["session"], turn_ids=[], by=w["alice"])
    assert reload(o).state == "withdrawn"


def test_a_removal_is_a_tombstone_not_a_toggle(w):
    o = emit(w, "mem", "person", w["alice"].pk, [w["source"]])
    consent.exclude(w["alice"], w["source"])
    cascade.sweep()
    consent.include(w["alice"], w["source"])                          # letting it back in ...
    cascade.sweep()
    assert reload(o).state == "withdrawn" and o.body == {}             # ... never un-withdraws
    assert cascade.emit_allowed(w["source"]) is True                   # new learning may use it again
    consent.exclude(w["alice"], w["source"])
    assert cascade.emit_allowed(w["source"]) is False                  # a replay cannot cite a removed source
    BrainSource.objects.filter(pk=w["source"].pk).update(state="withdrawn")
    consent.include(w["alice"], w["source"])
    w["source"].refresh_from_db()
    assert cascade.emit_allowed(w["source"]) is False


def test_a_withdrawn_output_is_seen_by_no_one(w):
    from apps.brain import outputs

    o = emit(w, "mem", "person", w["alice"].pk, [w["source"]])
    assert o in outputs.visible_to(w["alice"])
    consent.exclude(w["alice"], w["source"])
    cascade.sweep()
    assert o not in outputs.visible_to(w["alice"])


def test_a_stale_output_is_seen_by_no_one(w):
    from apps.brain import outputs

    other = sources.for_session(Session.objects.create(workspace=w["ws"], created_by=w["alice"],
                                                       origin=Session.ORIGIN_WEB))
    o = emit(w, "coach", "person", w["alice"].pk, [w["source"], other])
    assert o in outputs.visible_to(w["alice"])
    consent.exclude(w["alice"], w["source"])
    cascade.sweep()
    assert reload(o).state == "stale" and o not in outputs.visible_to(w["alice"])


def test_link_evidence_goes_with_a_removal_even_when_no_output_cites_it(w):
    SourceLink.objects.create(source=w["source"], subject="1", how="judged", tier="core", evidence=["alice: x"])
    cascade.sweep()
    assert SourceLink.objects.get().evidence == ["alice: x"]                     # nothing removed yet
    consent.exclude(w["bob"], w["source"])                                       # one participant takes it out
    cascade.sweep([w["source"].pk])
    assert SourceLink.objects.get().evidence == []


def test_the_conversation_of_a_run_that_read_a_removed_source_is_deleted(w):
    from apps.brain.models import BrainRun, BrainRunRead
    from apps.canopy_sessions.models import Message

    talk = Session.objects.create(workspace=w["ws"], origin=Session.ORIGIN_WEB, metadata={"brain_run": "1"})
    Message.objects.create(session=talk, role=Message.ASSISTANT, turn_index=0, plaintext="alice said: the secret plan")
    run = BrainRun.objects.create(capability=w["mem"], subject_type="person", subject=str(w["alice"].pk),
                                  purpose="remember", session=talk, state=BrainRun.DONE)
    BrainRunRead.objects.create(run=run, source=w["source"])
    cascade.sweep()
    assert Message.objects.filter(session=talk).exists()                           # nothing removed: kept
    consent.exclude(w["alice"], w["source"])
    assert cascade.sweep([w["source"].pk])["runs"] == 1
    talk.refresh_from_db()
    assert talk.content_deleted_at is not None and not Message.objects.filter(session=talk).exists()


def test_bob_s_removal_does_not_touch_a_run_about_alice(w):
    from apps.brain.models import BrainRun, BrainRunRead
    from apps.canopy_sessions.models import Message

    talk = Session.objects.create(workspace=w["ws"], origin=Session.ORIGIN_WEB, metadata={"brain_run": "1"})
    Message.objects.create(session=talk, role=Message.ASSISTANT, turn_index=0, plaintext="alice's own words")
    run = BrainRun.objects.create(capability=w["mem"], subject_type="person", subject=str(w["alice"].pk),
                                  purpose="remember", session=talk, state=BrainRun.DONE)
    BrainRunRead.objects.create(run=run, source=w["source"])
    consent.exclude(w["bob"], w["source"])
    assert cascade.sweep()["runs"] == 0 and Message.objects.filter(session=talk).exists()


def _memory_entry(w, who, session, statement="prefers short replies"):
    from apps.contacts import hcp
    from apps.contacts.models import EntryRelationship, PersonFact
    from apps.contacts.services import person_for

    person = person_for(user=who)
    fact = PersonFact.objects.create(person=person, workspace=w["ws"], kind="preference", statement=statement,
                                     embedding=b"\x00\x01", embedding_model="m")
    EntryRelationship.objects.create(person=person, entry_id=fact.entry_id, type="derivedFrom",
                                     source_kind="session", source_ref=str(session.pk),
                                     asserted_by_role="context-client", asserted_by_id="agent:x")
    assert hcp.current(fact.entry_id) is not None
    return fact


def test_a_memory_entry_learned_from_a_removed_session_is_deleted_with_its_embedding(w):
    fact = _memory_entry(w, w["alice"], w["session"])
    bobs = _memory_entry(w, w["bob"], w["session"], "likes tables")
    cascade.sweep()
    fact.refresh_from_db()
    assert fact.retracted_at is None and fact.embedding is not None              # nothing removed yet
    consent.exclude(w["alice"], w["source"])
    assert cascade.sweep([w["source"].pk])["memory_entries"] == 1
    fact.refresh_from_db()
    bobs.refresh_from_db()
    assert fact.retracted_at is not None and fact.status == "deleted"
    assert fact.embedding is None and fact.embedding_model == ""
    assert bobs.retracted_at is None                                             # bob's, from his own words, stays


def test_a_memory_entry_goes_when_its_person_drops_memory_or_its_words_are_deleted(w):
    first = _memory_entry(w, w["alice"], w["session"])
    consent.set_consent(w["alice"], classes=["individual"])                      # dropped memory
    cascade.sweep()
    first.refresh_from_db()
    assert first.retracted_at is not None
    carols = _memory_entry(w, w["carol"], w["session"])                          # carol never answered: kept
    cascade.sweep()
    carols.refresh_from_db()
    assert carols.retracted_at is None
    Session.objects.filter(pk=w["session"].pk).update(content_deleted_at=timezone.now())
    cascade.sweep()
    carols.refresh_from_db()
    assert carols.retracted_at is not None                                       # its words are gone


def test_another_store_is_asked_to_withdraw_or_mark(w):
    calls = []
    cascade.register_store("hcp.entry", lambda oid, mode, why: calls.append((oid, mode)))
    try:
        for s in (w["source"],):
            OutputLineage.objects.create(output_kind="hcp.entry", output_id="e1", source=s)
        Session.objects.filter(pk=w["session"].pk).update(content_deleted_at=timezone.now())
        cascade.sweep()
        assert calls == [("e1", "withdraw")]
    finally:
        cascade._STORES.pop("hcp.entry", None)


def test_runner_reship_after_deletion_is_still_refused(w):
    from apps.canopy_sessions import transcript_ingest

    Session.objects.filter(pk=w["session"].pk).update(content_deleted_at=timezone.now())
    w["session"].refresh_from_db()
    assert transcript_ingest.refuses_ship(w["session"])


def test_the_tick_is_registered():
    from apps.common import ticks

    assert {"brain.cascade", "brain.disclosure"} <= set(ticks.jobs())
    assert SessionParticipant  # imported for the fixtures


# ---- narrowing consent (fewer workspaces, a standing rule) reaches what was derived --------------------

def _second_workspace(w):
    from apps.workspaces.models import Workspace, WorkspaceMembership

    beta = Workspace.objects.create(slug="beta", display_name="Beta", created_by=w["owner"])
    for u in (w["alice"], w["bob"]):
        WorkspaceMembership.objects.create(user=u, workspace=beta, role=WorkspaceMembership.EDITOR)
    return beta


def test_narrowing_workspaces_withdraws_what_was_learned_about_that_person_only(w, django_capture_on_commit_callbacks):
    _second_workspace(w)
    about_alice = emit(w, "coach", "person", w["alice"].pk, [w["source"]])
    about_bob = emit(w, "coach", "person", w["bob"].pk, [w["source"]])
    shared = emit(w, "proj", "project", 1, [w["source"]])
    with django_capture_on_commit_callbacks(execute=True):                      # the act runs the sweep itself
        consent.set_consent(w["alice"], classes=["individual", "project", "memory"], workspaces=["beta"])
    assert reload(about_alice).state == "withdrawn" and reload(about_alice).body == {}
    assert reload(about_bob).state == "active"                                   # bob's own learning stays
    assert reload(shared).state == "withdrawn"                                   # a shared use relied on alice too


def test_a_standing_rule_withdraws_the_uses_it_now_denies(w):
    coach = emit(w, "coach", "person", w["alice"].pk, [w["source"]])
    mem = emit(w, "mem", "person", w["alice"].pk, [w["source"]])
    bobs = emit(w, "mem", "person", w["bob"].pk, [w["source"]])
    consent.set_consent(w["alice"], classes=["individual", "project", "memory"],
                        standing_rules=[{"class": "memory", "workspace": "acme"}])
    cascade.sweep()
    assert reload(mem).state == "withdrawn" and reload(coach).state == "active" and reload(bobs).state == "active"
    consent.set_consent(w["alice"], classes=["individual", "project", "memory"],
                        standing_rules=[{"capability": "coach"}])
    cascade.sweep()
    assert reload(coach).state == "withdrawn"


def test_a_rule_naming_a_purpose_matches_only_an_output_whose_run_had_it(w):
    from apps.brain.models import BrainRun

    run = BrainRun.objects.create(capability=w["coach"], subject_type="person", subject=str(w["alice"].pk),
                                  purpose="coach", state=BrainRun.DONE)
    o = emit(w, "coach", "person", w["alice"].pk, [w["source"]])
    BrainOutput.objects.filter(pk=o.pk).update(run=run)
    consent.set_consent(w["alice"], classes=["individual", "project", "memory"], standing_rules=[{"purpose": "review"}])
    cascade.sweep()
    assert reload(o).state == "active"                                           # a different purpose
    consent.set_consent(w["alice"], classes=["individual", "project", "memory"], standing_rules=[{"purpose": "coach"}])
    cascade.sweep()
    assert reload(o).state == "withdrawn"


# ---- a shared removal is anyone's, for good; a later joiner removes nothing already made ------------

def _project_run(w, words="alice said: the plan"):
    from apps.brain.models import BrainRun, BrainRunRead
    from apps.canopy_sessions.models import Message

    talk = Session.objects.create(workspace=w["ws"], origin=Session.ORIGIN_WEB, metadata={"brain_run": "9"})
    Message.objects.create(session=talk, role=Message.ASSISTANT, turn_index=0, plaintext=words)
    run = BrainRun.objects.create(capability=w["proj"], subject_type="project", subject="1", purpose="learn",
                                  session=talk, state=BrainRun.DONE)
    BrainRunRead.objects.create(run=run, source=w["source"])
    SourceLink.objects.create(source=w["source"], subject="1", how="judged", tier="core", evidence=["alice: x"])
    return talk


def test_a_shared_removal_outlives_the_remover_being_taken_out_of_the_session(w):
    """Bob removes it; before any sweep runs, the owner takes bob (who never wrote) out of the session, so his
    participant row goes. The removal still reaches the run conversation, the link evidence and shared outputs."""
    from apps.canopy_sessions.models import Message

    talk = _project_run(w)
    shared = emit(w, "proj", "project", 1, [w["source"]], scope=[w["alice"].pk])
    consent.exclude(w["bob"], w["source"])
    SessionParticipant.objects.filter(session=w["session"], user=w["bob"]).delete()
    sources.refresh(w["source"])
    assert not w["source"].participants.filter(user=w["bob"]).exists()
    out = cascade.sweep()
    assert out["runs"] == 1 and not Message.objects.filter(session=talk).exists()
    assert SourceLink.objects.get().evidence == []
    assert reload(shared).state == "withdrawn"


def test_someone_joining_without_consent_closes_reads_but_destroys_nothing_already_made(w):
    from apps.brain.eligibility import eligible
    from apps.canopy_sessions.models import Message

    talk = _project_run(w)
    SessionParticipant.objects.create(session=w["session"], user=w["carol"])     # carol never agreed to anything
    sources.refresh(w["source"])
    assert not eligible(w["source"], w["proj"], "learn")                        # no new read
    out = cascade.sweep()
    assert out["runs"] == 0 and Message.objects.filter(session=talk).exists()   # the run's record stays
    assert SourceLink.objects.get().evidence == ["alice: x"]
    consent.revoke(w["alice"])                                                  # someone who agreed stops: it goes
    assert cascade.sweep()["runs"] == 1
    assert SourceLink.objects.get().evidence == []


# ---- a memory entry: only for a user who once agreed, or by their own removal or the words' deletion ---

def test_a_contacts_memory_entry_is_never_deleted_by_the_cascade(w):
    from apps.contacts import hcp
    from apps.contacts.models import Contact, EntryRelationship, PersonFact
    from apps.contacts.services import person_for

    c = Contact.objects.create(workspace=w["ws"], email="z@example.org")
    chat = Session.objects.create(workspace=w["ws"], contact=c, origin=Session.ORIGIN_WEB)      # sealed: a contact's
    person = person_for(contact=c)
    fact = PersonFact.objects.create(person=person, workspace=w["ws"], kind="preference", statement="tea, not coffee")
    EntryRelationship.objects.create(person=person, entry_id=fact.entry_id, type="derivedFrom", source_kind="session",
                                     source_ref=str(chat.pk), asserted_by_role="context-client", asserted_by_id="agent:x")
    assert hcp.current(fact.entry_id) is not None
    assert cascade.sweep()["memory_entries"] == 0
    fact.refresh_from_db()
    assert fact.retracted_at is None


def test_a_sensitive_session_deletes_memory_only_for_someone_who_once_agreed(w):
    from apps.harness import runner_requirements as rr

    Session.objects.filter(pk=w["session"].pk).update(metadata={rr.METADATA_KEY: ["sensitive-data"]})
    carols = _memory_entry(w, w["carol"], w["session"])                         # carol never answered: kept
    alices = _memory_entry(w, w["alice"], w["session"], "likes tables")          # alice agreed to memory: goes
    assert cascade.sweep()["memory_entries"] == 1
    carols.refresh_from_db()
    alices.refresh_from_db()
    assert carols.retracted_at is None and alices.retracted_at is not None


def test_a_person_who_never_agreed_still_removes_their_own_memory_by_excluding_the_session(w):
    carols = _memory_entry(w, w["carol"], w["session"])
    SessionParticipant.objects.create(session=w["session"], user=w["carol"])
    sources.refresh(w["source"])
    consent.exclude(w["carol"], w["source"])
    assert cascade.sweep()["memory_entries"] == 1
    carols.refresh_from_db()
    assert carols.retracted_at is not None
