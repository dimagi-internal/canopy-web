"""A session's mode is fixed when it starts (apps/harness/turn_mode.py).

Jonathan, 2026-10-07: "the entire session is started in auto or manual, it doesn't
change because of something that happens in the session." Ada's dispatches from ACE
ran manual and stopped for approval; his "yes merge it" in the same chat then ran
auto, judged afresh as the owner. Now a later turn takes the session's first mode —
but only downward: an auto start never lifts a turn its own sender would hold manual.
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.canopy_sessions.services import with_driving_turn
from apps.harness import turn_mode as modes
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def ada():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=jj,
                                 turn_mode=Agent.AUTO)
    session = Session.objects.create(workspace=ws, agent=agent, origin=Session.ORIGIN_RUNNER,
                                     title="dispatch")
    RunnerBinding.objects.create(session=session, session_key="claude-1")
    return {"agent": agent, "session": session}


def _turn(ada, *, mode="", chat=True, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, key=""):
    return Turn.objects.create(
        agent=None if chat else ada["agent"], chat_session=ada["session"] if chat else None,
        origin=origin, idempotency_key=uuid.uuid4().hex, turn_mode=mode,
        session_key=key,
    )


def test_a_reply_in_a_session_that_started_manual_stays_manual(ada):
    _turn(ada, mode=modes.MANUAL, chat=False, origin=Turn.ORIGIN_API, key="claude-1")
    reply = _turn(ada)  # the owner's "yes merge it" — auto on its own
    assert modes.for_turn(reply, {}, fresh=True) == modes.Resolved(
        modes.MANUAL, "session started manual")


def test_a_session_that_started_auto_never_lifts_a_turn_past_its_own_mode(ada):
    _turn(ada, mode=modes.AUTO)
    Agent.objects.filter(pk=ada["agent"].pk).update(turn_mode=Agent.MANUAL)
    later = _turn(ada)
    later.chat_session.agent.refresh_from_db()
    assert modes.for_turn(later, {}, fresh=True).mode == modes.MANUAL


def test_the_first_turn_is_judged_on_its_own(ada):
    first = _turn(ada)
    assert modes.for_turn(first, {}, fresh=True) == modes.Resolved(modes.AUTO, "agent")


def test_the_session_reports_the_mode_it_started_in(ada):
    _turn(ada, mode=modes.MANUAL, chat=False, origin=Turn.ORIGIN_API, key="claude-1")
    _turn(ada, mode=modes.AUTO)  # a reply stamped before this rule existed
    row = with_driving_turn(Session.objects.filter(pk=ada["session"].pk)).get()
    assert (row._turn_mode, row._turn_origin) == (modes.MANUAL, Turn.ORIGIN_API)
