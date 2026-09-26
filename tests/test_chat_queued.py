"""Sends not yet in the transcript, shown to everyone, in send order."""
import uuid

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.canopy_sessions import authorship
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Message
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _runner_session():
    """A transcript-sourced session: the path where a send writes no durable row."""
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jon")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    session = chat.create_session(workspace=ws, created_by=owner)
    # Make it transcript-sourced — see services.transcript_sourced for the flag
    # this needs.
    session.metadata = {**(session.metadata or {}), chat.TRANSCRIPT_SOURCED: True}
    session.save(update_fields=["metadata"])
    return owner, session


def test_queued_lists_every_pending_send_in_order():
    owner, session = _runner_session()
    chat.send_message(session=session, text="first", user=owner, client_id="c1")
    chat.send_message(session=session, text="second", user=owner, client_id="c2")
    q = chat.queued_messages(session)
    assert [e["text"] for e in q] == ["first", "second"]
    assert [e["client_id"] for e in q] == ["c1", "c2"]
    assert q[0]["author"] == {"name": "Jon", "user_id": owner.id}
    assert q[0]["state"] == "queued"


def test_a_send_leaves_the_list_when_its_transcript_row_lands():
    owner, session = _runner_session()
    _m, turn = chat.send_message(session=session, text="first", user=owner, client_id="c1")
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user",
        "text": authorship.mark("first", name="Jon", user_id=owner.id, turn_id=turn.pk)}])
    assert chat.queued_messages(session) == []


def test_terminal_turns_are_not_queued():
    owner, session = _runner_session()
    _m, turn = chat.send_message(session=session, text="first", user=owner, client_id="c1")
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CANCELLED)
    assert chat.queued_messages(session) == []


def test_claimed_turn_is_delivering():
    owner, session = _runner_session()
    _m, turn = chat.send_message(session=session, text="first", user=owner, client_id="c1")
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CLAIMED)
    assert chat.queued_messages(session)[0]["state"] == "delivering"


def test_query_count_does_not_grow_with_session_history():
    """This runs on every status transition and every streamed transcript
    batch, so it must be bounded by the NON-TERMINAL turns, never by how many
    Messages the session has ever landed."""
    owner, session = _runner_session()
    # One turn currently queued — kept out of `landed` — so both measurements
    # exercise the real query path rather than the `turns` empty-list shortcut.
    chat.send_message(session=session, text="active", user=owner, client_id="active")

    with CaptureQueriesContext(connection) as ctx:
        chat.queued_messages(session)
    baseline = len(ctx.captured_queries)

    # ~20 already-landed historical rows. The old implementation's `landed`
    # query scanned every Message with a non-null source_turn_id in the
    # session, so this would have grown the query's RESULT SET (not its
    # count) — cheap to miss in a small test, real at 19k rows in production.
    for i in range(20):
        Message.objects.create(
            session=session, turn_index=1000 + i, role=Message.USER,
            plaintext=f"h{i}", content={}, source_turn_id=uuid.uuid4(),
        )

    with CaptureQueriesContext(connection) as ctx:
        chat.queued_messages(session)
    assert len(ctx.captured_queries) == baseline
