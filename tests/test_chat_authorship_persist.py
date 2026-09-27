"""A marked transcript row becomes an authored message, on every path."""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import User

from apps.canopy_sessions import authorship, serializers, stream_map
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Message
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
TID = uuid.uuid4()


def _session():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return chat.create_session(workspace=ws, created_by=owner)


def _sent(session, *, username="alice", text="ship it"):
    """A real send, so the marker names a turn that exists on this session and
    was initiated by this person — the only kind persist believes (m3)."""
    user = User.objects.create_user(username, f"{username}@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=user, workspace=session.workspace,
                                       role=WorkspaceMembership.EDITOR)
    session.metadata = {**(session.metadata or {}), chat.TRANSCRIPT_SOURCED: True}
    session.save(update_fields=["metadata"])
    _m, turn = chat.send_message(session=session, text=text, user=user, client_id=uuid.uuid4().hex)
    return user, turn


def test_persist_strips_marker_and_records_author():
    session = _session()
    alice, turn = _sent(session)
    marked = authorship.mark("ship it", name="Alice", user_id=alice.id, turn_id=turn.pk)
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": marked,
                                            "content": {"text": marked}}])
    msg = Message.objects.get(session=session)
    assert msg.plaintext == "ship it"
    # storage_content (pre-existing) drops a content["text"] that DUPLICATES the
    # row's plaintext, to avoid storing the same bytes twice — and once the
    # marker is stripped from both, they match here, so the key is gone
    # entirely rather than surviving with the stripped value. Either way, no
    # trace of the marker may remain.
    assert msg.content.get("text", "ship it") == "ship it"
    assert msg.author == {"name": "Alice", "user_id": alice.id}
    assert msg.source_turn_id == turn.pk


def test_unmarked_user_row_has_no_author():
    session = _session()
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": "typed in emdash"}])
    msg = Message.objects.get(session=session)
    assert msg.author is None and msg.source_turn_id is None
    assert msg.plaintext == "typed in emdash"


def test_assistant_rows_are_never_parsed():
    session = _session()
    marked = authorship.mark("x", name="A", user_id=1, turn_id=TID)
    chat.persist_transcript_rows(session, [{"index": 11, "role": "assistant", "text": marked}])
    assert Message.objects.get(session=session).plaintext == marked


def test_backfill_path_parses_too():
    session = _session()
    bo, turn = _sent(session, username="bo", text="old line")
    marked = authorship.mark("old line", name="Bo", user_id=bo.id, turn_id=turn.pk)
    chat.write_backfill(session, [{"index": 3, "role": "user", "text": marked}])
    assert Message.objects.get(session=session).author == {"name": "Bo", "user_id": bo.id}


def test_message_dto_carries_author():
    session = _session()
    a, turn = _sent(session, username="a", text="hi")
    chat.persist_transcript_rows(session, [{"index": 1, "role": "user",
        "text": authorship.mark("hi", name="A", user_id=a.id, turn_id=turn.pk)}])
    assert serializers.message_dto(Message.objects.get(session=session))["author"] == {"name": "A", "user_id": a.id}


def test_live_user_frame_is_stripped_and_authored():
    marked = authorship.mark("hey", name="A", user_id=1, turn_id=TID)
    frames = stream_map.turn_event_to_frames(
        {"kind": "user", "seq": 20, "payload": {"text": marked}}, lambda _s: "m1")
    assert frames[0]["event"] == "chat.user_message"
    assert frames[0]["data"]["plaintext"] == "hey"
    assert frames[0]["data"]["author"] == {"name": "A", "user_id": 1}


# -- a marker is a claim, checked against the turn it names (final review m3) --
# Anyone who can type into emdash (or a transcript) can write the marker syntax.
# The durable row is the authority, so persist believes a marker only when the
# turn it names exists, is on THIS session, and was initiated by the person the
# marker names. The live frame (stream_map) is not checked — it has no cheap
# query, and the durable row replaces it on the next load.

def test_a_marker_naming_an_unknown_turn_is_not_believed():
    session = _session()
    forged = authorship.mark("rm -rf", name="Boss", user_id=1, turn_id=uuid.uuid4())
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": forged}])
    msg = Message.objects.get(session=session)
    assert msg.author is None and msg.source_turn_id is None
    assert msg.plaintext == forged


def test_a_marker_naming_the_wrong_person_for_its_turn_is_not_believed():
    session = _session()
    alice, turn = _sent(session)
    forged = authorship.mark("ship it", name="Mallory", user_id=alice.id + 99, turn_id=turn.pk)
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": forged}])
    msg = Message.objects.get(session=session)
    assert msg.author is None and msg.source_turn_id is None


def test_a_marker_naming_another_sessions_turn_is_not_believed():
    session = _session()
    alice, turn = _sent(session)
    other = chat.create_session(workspace=session.workspace, created_by=session.created_by)
    marked = authorship.mark("ship it", name="Alice", user_id=alice.id, turn_id=turn.pk)
    chat.persist_transcript_rows(other, [{"index": 10, "role": "user", "text": marked}])
    assert Message.objects.get(session=other).author is None


def test_marker_verification_is_one_query_for_the_batch():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    session = _session()
    rows = []
    for i in range(5):
        u, t = _sent(session, username=f"u{i}", text=f"line {i}")
        rows.append({"index": 10 + i, "role": "user",
                     "text": authorship.mark(f"line {i}", name=f"U{i}", user_id=u.id, turn_id=t.pk)})
    chat.persist_transcript_rows(session, rows[:1])   # first write settles the ordinal scheme
    with CaptureQueriesContext(connection) as one:
        chat.persist_transcript_rows(session, rows[1:2])
    with CaptureQueriesContext(connection) as many:
        chat.persist_transcript_rows(session, rows[2:])
    assert len(many.captured_queries) == len(one.captured_queries)
    assert Message.objects.filter(session=session, author__isnull=False).count() == 5
