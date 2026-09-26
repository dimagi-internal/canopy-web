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


def test_persist_strips_marker_and_records_author():
    session = _session()
    marked = authorship.mark("ship it", name="Alice", user_id=42, turn_id=TID)
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
    assert msg.author == {"name": "Alice", "user_id": 42}
    assert msg.source_turn_id == TID


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
    marked = authorship.mark("old line", name="Bo", user_id=5, turn_id=TID)
    chat.write_backfill(session, [{"index": 3, "role": "user", "text": marked}])
    assert Message.objects.get(session=session).author == {"name": "Bo", "user_id": 5}


def test_message_dto_carries_author():
    session = _session()
    chat.persist_transcript_rows(session, [{"index": 1, "role": "user",
        "text": authorship.mark("hi", name="A", user_id=1, turn_id=TID)}])
    assert serializers.message_dto(Message.objects.get(session=session))["author"] == {"name": "A", "user_id": 1}


def test_live_user_frame_is_stripped_and_authored():
    marked = authorship.mark("hey", name="A", user_id=1, turn_id=TID)
    frames = stream_map.turn_event_to_frames(
        {"kind": "user", "seq": 20, "payload": {"text": marked}}, lambda _s: "m1")
    assert frames[0]["event"] == "chat.user_message"
    assert frames[0]["data"]["plaintext"] == "hey"
    assert frames[0]["data"]["author"] == {"name": "A", "user_id": 1}
