"""SP2a Task 4 — the stub executor drives a queued session turn to done."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.executor import execute_turn_stub
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    # The owner is a member: an agent owner outside the workspace is an outsider
    # to it (`relationship_for_user`), and their sends are refused.
    WorkspaceMembership.objects.create(workspace=ws, user=user, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    session = chat.create_session(workspace=ws, created_by=user, agent=agent)
    return user, session


def test_stub_executes_queued_session_turn_end_to_end():
    user, session = _ctx()
    _msg, turn = chat.send_message(session=session, text="hi", user=user)
    assert turn.status == Turn.QUEUED

    result = execute_turn_stub(turn, reply="hello back")
    assert result.status == Turn.DONE

    # Transcript: the user message plus the projected assistant reply.
    rows = list(session.messages.order_by("turn_index"))
    assert [m.role for m in rows] == ["user", "assistant"]
    assert rows[1].plaintext == "hello back"

    # The ledger carries the runner's real event shape: a status open, the
    # assistant event, then a terminal status.
    kinds = [e.kind for e in turn.events.order_by("seq")]
    assert kinds[0] == "status"
    assert "assistant" in kinds


def test_stub_is_a_noop_on_an_already_finished_turn():
    user, session = _ctx()
    _msg, turn = chat.send_message(session=session, text="hi", user=user)
    execute_turn_stub(turn)
    count_after_first = session.messages.count()
    execute_turn_stub(turn)  # already DONE -> no-op
    assert session.messages.count() == count_after_first
