"""One identity for a transcript row, from runner to screen.

The stored `Message` is the truth: a live frame names the row the server stored —
its `turn_index` (the shipped ordinal shifted into the session's epoch) and its
id — so a row that arrived live sorts where a reload puts it and is the same row
on the client. The epoch shift happens in exactly one place, under the binding
lock, whichever path the rows came by.
"""
from __future__ import annotations

import pytest
from canopy_transcript import BLOCK_STRIDE
from django.contrib.auth.models import User
from django.utils import timezone

from apps.canopy_sessions import autotitle, serializers, stream_map, transcript_ingest, transcript_read
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness import initiator as who
from apps.harness import services as harness_services
from apps.harness import session_sync
from apps.harness.models import Runner, Turn
from apps.harness.schemas import LiveEventIn
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _ctx(*, transcript_id="t1", index_offset=0):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    runner = Runner.objects.create(name="mbp", kind=Runner.EMDASH, owner=user, host="mbp.local",
                                   capabilities={}, workspace=ws, status=Runner.ONLINE)
    session = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="bednet")
    binding = RunnerBinding.objects.create(
        session=session, runner=runner, session_key="bednet", host="mbp.local",
        transcript_id=transcript_id, index_offset=index_offset, stream_desired=True)
    return user, session, binding


def _event(kind, index, text="", **payload):
    return LiveEventIn(kind=kind, seq=index, index=index, payload={"text": text, **payload})


def _frames(published):
    return [m["event"] for _g, m in published if m.get("type") == "chat.turn_event"]


@pytest.fixture
def published(monkeypatch):
    out = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: out.append((g, m)))
    return out


def test_a_live_frame_carries_the_stored_turn_index_and_id_after_an_epoch(published):
    _user, session, binding = _ctx(index_offset=3 * BLOCK_STRIDE)
    session_sync.ingest_session_stream(
        binding, transcript_id="t1", events=[_event("assistant", 0, "hello")])

    row = Message.objects.get(session=session)
    assert row.turn_index == 3 * BLOCK_STRIDE
    (event,) = _frames(published)
    assert event["turn_index"] == row.turn_index
    assert event["message_id"] == str(row.pk)
    # …and the client frame built from it is the reloaded row's identity.
    frames = stream_map.turn_event_to_frames(event, lambda _o: event["message_id"])
    assert frames[0]["data"] == {"message_id": str(row.pk), "turn_index": row.turn_index}


def test_a_new_transcript_lands_in_its_new_epoch_and_the_live_frame_says_so(published):
    user, session, binding = _ctx(transcript_id="t1")
    session_sync.ingest_session_stream(
        binding, transcript_id="t1", events=[_event("assistant", 0, "old reply")])
    turn, _ = harness_services.enqueue_turn(
        session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
        idempotency_key=f"chat:{session.id.hex}:c1", prompt="next please",
        origin_ref={"client_id": "nonce-1"},
        initiator=who.for_user(user, via="web", assurance="verified"))
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE, claimed_at=timezone.now())
    published.clear()

    # A new file: its ordinal 0 must land above the held rows, and the author and
    # nonce the stored row carries must reach the live frame.
    binding.refresh_from_db()
    session_sync.ingest_session_stream(
        binding, transcript_id="t2", events=[_event("user", 0, "next please")])

    binding.refresh_from_db()
    assert binding.index_offset == 2 * BLOCK_STRIDE
    row = Message.objects.get(session=session, role=Message.USER)
    assert row.turn_index == 2 * BLOCK_STRIDE
    (event,) = _frames(published)
    assert event["turn_index"] == row.turn_index and event["message_id"] == str(row.pk)
    assert event["payload"]["author"] == {"name": "Jonathan", "user_id": user.id}
    assert event["payload"]["client_id"] == "nonce-1"


def test_a_confined_turns_rows_are_shifted_once(published):
    """A confined turn's epoch is already absolute; the binding's own offset is
    not added on top of it."""
    _user, session, binding = _ctx(index_offset=5 * BLOCK_STRIDE)
    transcript_ingest.persist_transcript_rows(
        session, [{"index": 0, "role": "assistant", "text": "bound reply"}])
    held = Message.objects.get(session=session).turn_index
    assert held == 5 * BLOCK_STRIDE

    session_sync.ingest_turn_end_rows(
        session, transcript_id="confined-1", events=[_event("assistant", 0, "confined reply")])

    epoch = (held // BLOCK_STRIDE + 2) * BLOCK_STRIDE
    row = Message.objects.get(session=session, plaintext="confined reply")
    assert row.turn_index == epoch
    (event,) = _frames(published)
    assert event["turn_index"] == epoch and event["message_id"] == str(row.pk)
    # A re-ship of the same turn adds nothing and shows nothing again.
    published.clear()
    session_sync.ingest_turn_end_rows(
        session, transcript_id="confined-1", events=[_event("assistant", 0, "confined reply")])
    assert Message.objects.filter(session=session).count() == 2
    assert _frames(published) == []


def test_a_live_only_event_keeps_its_seq(published):
    _user, _session, binding = _ctx()
    session_sync.ingest_session_stream(
        binding, transcript_id="t1",
        events=[LiveEventIn(kind="activity:working", seq=7, index=-1, payload={})])
    (event,) = _frames(published)
    assert event["seq"] == 7 and "turn_index" not in event


def test_a_new_session_starts_on_the_current_ordinal_scheme():
    _user, session, _binding = _ctx()
    assert session.ordinal_scheme == transcript_ingest.ORDINAL_SCHEME


def test_the_first_write_to_a_new_session_keeps_what_it_holds():
    """A new session is not "an old scheme": its first transcript write must not
    wipe a row it already holds."""
    _user, session, _binding = _ctx()
    Message.objects.create(session=session, turn_index=0, role=Message.ASSISTANT, plaintext="kept")
    transcript_ingest.persist_transcript_rows(
        session, [{"index": 1, "role": "assistant", "text": "new"}])
    assert sorted(session.messages.values_list("plaintext", flat=True)) == ["kept", "new"]


# -- what a person typed: decided once, at ingest -------------------------------------

def test_ingest_marks_the_rows_a_person_typed():
    _user, session, _binding = _ctx()
    transcript_ingest.persist_transcript_rows(session, [
        {"index": 0, "role": "user", "text": "/hal:turn",
         "content": {"text": "/hal:turn", "record": {}}},
        {"index": BLOCK_STRIDE, "role": "user", "text": "# Turn procedure\n1. read the inbox",
         "content": {"text": "# Turn procedure\n1. read the inbox", "record": {"meta": True}}},
        {"index": 2 * BLOCK_STRIDE, "role": "user", "text": "This session is being continued",
         "content": {"text": "x", "record": {"compact_summary": True}}},
        {"index": 3 * BLOCK_STRIDE, "role": "assistant", "text": "on it"},
    ])
    typed = dict(session.messages.values_list("turn_index", "typed"))
    assert typed == {0: True, BLOCK_STRIDE: False, 2 * BLOCK_STRIDE: False, 3 * BLOCK_STRIDE: False}


def test_a_programs_prompt_is_not_typed():
    _user, session, _binding = _ctx()
    harness_services.enqueue_turn(session=session, origin=Turn.ORIGIN_CANOPY_SCHEDULER,
                                  idempotency_key="sched-1", prompt="/hal:turn nightly")
    transcript_ingest.persist_transcript_rows(session, [
        {"index": 0, "role": "user", "text": "/hal:turn nightly"},
        {"index": BLOCK_STRIDE, "role": "user", "text": "what did you find?"},
    ])
    assert dict(session.messages.values_list("plaintext", "typed")) == {
        "/hal:turn nightly": False, "what did you find?": True}


def test_the_opening_and_the_autotitle_name_the_first_typed_message():
    _user, session, binding = _ctx()
    binding.delete()          # unbound: autotitle names it from its first message
    session.title = ""
    session.save(update_fields=["title"])
    transcript_ingest.persist_transcript_rows(session, [
        {"index": 0, "role": "user", "text": "# Turn procedure",
         "content": {"record": {"meta": True}}},
        {"index": BLOCK_STRIDE, "role": "user", "text": "  find the  bednet runs  "},
    ])
    assert transcript_read.opening_of(
        transcript_read.with_opening(Session.objects.filter(pk=session.pk)).get()) == "find the bednet runs"
    assert autotitle.maybe_autotitle(session.pk) == "find the bednet runs"
    assert autotitle.title_from("  find the  bednet runs  ") == "find the bednet runs"


# -- newest is by turn_index ----------------------------------------------------------

def test_last_activity_is_the_newest_row_by_turn_index_not_the_newest_insert():
    _user, session, _binding = _ctx()
    transcript_ingest.persist_transcript_rows(session, [{"index": 10, "role": "assistant", "text": "late"}])
    newest = Message.objects.get(session=session)
    # A backfill writes older history afterwards.
    transcript_ingest.persist_transcript_rows(session, [{"index": 0, "role": "assistant", "text": "early"}])
    annotated = transcript_read.with_activity(Session.objects.filter(pk=session.pk)).get()
    assert annotated._last_msg_at == newest.created_at


# -- one message shape for REST and the socket ----------------------------------------

def test_rest_and_socket_give_a_row_one_shape():
    from apps.canopy_sessions.schemas import MessageOut

    _user, session, _binding = _ctx()
    transcript_ingest.persist_transcript_rows(session, [{"index": 0, "role": "assistant", "text": "hi"}])
    row = Message.objects.get(session=session)
    rest = MessageOut.from_orm(row).model_dump(mode="json")
    socket = serializers.message_dto(row)
    assert rest["id"] == socket["id"] == str(row.pk)
    shared = {k: v for k, v in rest.items() if v is not None}
    assert {k: socket[k] for k in shared} == shared


def test_the_backfill_migration_marks_existing_rows():
    """Rows stored before `typed` existed are marked by the same rule. (Delete once
    a deploy has run 0065.)"""
    import importlib

    from django.apps import apps as live_apps

    _user, session, _binding = _ctx()
    for i, (text, record) in enumerate([("hello there", {}), ("# skill body", {"meta": True})]):
        Message.objects.create(session=session, turn_index=i * BLOCK_STRIDE, role=Message.USER,
                               plaintext=text, content={"record": record})
    migration = importlib.import_module("apps.canopy_sessions.migrations.0065_backfill_message_typed")
    migration.backfill(live_apps, None)
    assert dict(session.messages.values_list("plaintext", "typed")) == {
        "hello there": True, "# skill body": False}
