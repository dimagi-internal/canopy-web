"""Sends not yet in the transcript, shown to everyone, in send order."""
import uuid

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.canopy_sessions import services as chat
from apps.canopy_sessions import send as session_send
from apps.canopy_sessions import transcript_ingest as session_transcript_ingest
from apps.canopy_sessions.models import Message
from apps.canopy_sessions.testing import legacy_marker
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
    session_send.send_message(session=session, text="first", user=owner, client_id="c1")
    session_send.send_message(session=session, text="second", user=owner, client_id="c2")
    q = session_send.queued_messages(session)
    assert [e["text"] for e in q] == ["first", "second"]
    assert [e["client_id"] for e in q] == ["c1", "c2"]
    assert q[0]["author"] == {"name": "Jon", "user_id": owner.id}
    assert q[0]["state"] == "queued"


def test_a_send_leaves_the_list_when_its_transcript_row_lands():
    owner, session = _runner_session()
    _m, turn = session_send.send_message(session=session, text="first", user=owner, client_id="c1")
    session_transcript_ingest.persist_transcript_rows(session, [{"index": 10, "role": "user",
        "text": legacy_marker("first", name="Jon", user_id=owner.id, turn_id=turn.pk)}])
    assert session_send.queued_messages(session) == []


def test_terminal_turns_are_not_queued():
    owner, session = _runner_session()
    _m, turn = session_send.send_message(session=session, text="first", user=owner, client_id="c1")
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CANCELLED)
    assert session_send.queued_messages(session) == []


def test_claimed_turn_is_delivering():
    owner, session = _runner_session()
    _m, turn = session_send.send_message(session=session, text="first", user=owner, client_id="c1")
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CLAIMED)
    assert session_send.queued_messages(session)[0]["state"] == "delivering"


def test_query_count_does_not_grow_with_session_history():
    """This runs on every status transition and every streamed transcript
    batch, so it must be bounded by the NON-TERMINAL turns, never by how many
    Messages the session has ever landed."""
    owner, session = _runner_session()
    # One turn currently queued — kept out of `landed` — so both measurements
    # exercise the real query path rather than the `turns` empty-list shortcut.
    session_send.send_message(session=session, text="active", user=owner, client_id="active")

    with CaptureQueriesContext(connection) as ctx:
        session_send.queued_messages(session)
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
        session_send.queued_messages(session)
    assert len(ctx.captured_queries) == baseline


def test_an_email_turn_on_the_session_is_not_a_queued_human_line():
    from apps.agents.models import Agent
    from apps.harness import initiator as who
    from apps.harness import services as harness

    owner, session = _runner_session()
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=session.workspace)
    turn, _ = harness.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key="email:1",
        prompt="/echo:turn --thread abc",
        origin_ref={"from": "jj@dimagi.com", "subject": "s", "thread_id": "abc"},
        initiator=who.for_user(owner, via="email", assurance="dmarc"),
    )
    email_session = turn.chat_session
    assert email_session is not None
    assert session_send.queued_messages(email_session) == []


def test_a_send_without_a_client_id_reports_none():
    """The key suffix of a no-nonce send is an index or a server nonce, not
    anything a client sent — reporting it as `client_id` would never match."""
    owner, session = _runner_session()
    session_send.send_message(session=session, text="first", user=owner)
    assert session_send.queued_messages(session)[0]["client_id"] == ""


# ── withdrawing one queued send ────────────────────────────────────────────────

def test_you_can_withdraw_your_own_queued_send_and_only_that_one():
    owner, session = _runner_session()
    _m, first = session_send.send_message(session=session, text="first", user=owner, client_id="c1")
    session_send.send_message(session=session, text="second", user=owner, client_id="c2")
    assert session_send.unqueue_send(session, first.pk, owner, by="Jon") is True
    first.refresh_from_db()
    assert first.status == Turn.CANCELLED
    assert [e["text"] for e in session_send.queued_messages(session)] == ["second"]


def test_a_claimed_send_cannot_be_withdrawn():
    """Once a runner holds it the words may already be in the agent's input."""
    owner, session = _runner_session()
    _m, turn = session_send.send_message(session=session, text="first", user=owner, client_id="c1")
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CLAIMED)
    assert session_send.unqueue_send(session, turn.pk, owner) is False
    turn.refresh_from_db()
    assert turn.status == Turn.CLAIMED


def test_a_teammates_queued_send_is_theirs_to_withdraw():
    owner, session = _runner_session()
    mate = User.objects.create_user("mate", "mate@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=mate, workspace=session.workspace,
                                       role=WorkspaceMembership.OWNER)
    _m, turn = session_send.send_message(session=session, text="first", user=owner, client_id="c1")
    assert session_send.unqueue_send(session, turn.pk, mate) is False
    assert session_send.unqueue_send(session, "not-a-uuid", owner) is False
    assert session_send.unqueue_send(session, uuid.uuid4(), owner) is False
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


def test_a_withdrawn_send_is_listed_as_cancelled_in_send_order():
    """canopy-web#2092: A is withdrawn, B is sent and answered. A must stay a
    first-class entry — ordered by when it was sent, saying who cancelled it —
    not vanish from the server's view (and linger in a client as a live row)."""
    owner, session = _runner_session()
    _m, a = session_send.send_message(session=session, text="A", user=owner, client_id="ca")
    assert session_send.unqueue_send(session, a.pk, owner, by="Jon") is True
    _m, b = session_send.send_message(session=session, text="B", user=owner, client_id="cb")
    session_transcript_ingest.persist_transcript_rows(session, [{"index": 10, "role": "user",
        "text": legacy_marker("B", name="Jon", user_id=owner.id, turn_id=b.pk)}])
    assert session_send.queued_messages(session) == []
    cancelled = session_send.ended_sends(session)
    assert [(c["text"], c["client_id"], c["cancelled_by"]) for c in cancelled] == [("A", "ca", "Jon")]
    assert cancelled[0]["sent_at"] < b.created_at.isoformat()
    assert cancelled[0]["author"] == {"name": "Jon", "user_id": owner.id}


def test_a_send_that_reached_the_transcript_is_not_reported_cancelled():
    owner, session = _runner_session()
    _m, t = session_send.send_message(session=session, text="A", user=owner, client_id="ca")
    session_transcript_ingest.persist_transcript_rows(session, [{"index": 10, "role": "user",
        "text": legacy_marker("A", name="Jon", user_id=owner.id, turn_id=t.pk)}])
    Turn.objects.filter(pk=t.pk).update(status=Turn.CANCELLED)
    assert session_send.ended_sends(session) == []
