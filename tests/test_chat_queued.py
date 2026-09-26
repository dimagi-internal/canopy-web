"""Sends not yet in the transcript, shown to everyone, in send order."""
import pytest
from django.contrib.auth.models import User

from apps.canopy_sessions import authorship
from apps.canopy_sessions import services as chat
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
