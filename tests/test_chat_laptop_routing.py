"""Slice-3 groundwork: a chat session turn carries the continuity thread_key and
surfaces the session's agent as the emdash target the runner drives."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.send import send_message
from apps.harness.schemas import TurnOut
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _seed(*, with_agent=True):
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=u)
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.OWNER)
    a = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=u) if with_agent else None
    s = chat.create_session(workspace=ws, created_by=u, agent=a)
    return u, s, a


def test_send_sets_thread_key_to_session_id():
    u, s, _a = _seed()
    _msg, turn = send_message(session=s, text="hi", user=u)
    assert turn.origin_ref.get("thread_key") == str(s.id)
    # chat_session_id marks it as a chat turn the runner should bridge back.
    assert turn.origin_ref.get("chat_session_id") == str(s.id)


def test_session_turn_exposes_session_agent_slug():
    u, s, a = _seed(with_agent=True)
    _msg, turn = send_message(session=s, text="hi", user=u)
    out = TurnOut.from_orm(turn)
    assert out.agent_slug == a.slug


def test_agentless_session_turn_has_no_agent_slug():
    u, s, _a = _seed(with_agent=False)
    _msg, turn = send_message(session=s, text="hi", user=u)
    out = TurnOut.from_orm(turn)
    assert out.agent_slug is None
