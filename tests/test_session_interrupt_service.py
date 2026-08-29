"""Stopping a session that no Turn owns.

The turn-shaped stop reaches chat and nothing else: an agent, board or scheduled
turn is fire-and-continue — `execute_turn` finishes it the moment the prompt is
delivered — so seconds later the agent is working hard on a turn that is already
DONE. Stop keyed on turns found nothing, did nothing, and did not even flicker.

They are all sessions, and canopy already knows the session: `record_session` gives
every agent/project/phone thread a durable Session plus a RunnerBinding carrying the
emdash task. So stop them as sessions.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws,
                                       role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner)
    # An agent session, exactly as harness.services._thread_session builds one for a
    # board/scheduled turn: origin=runner, no chat messages, no live Turn.
    session = Session.objects.create(agent=agent, workspace=ws,
                                     origin=Session.ORIGIN_RUNNER, title="hal:turn")
    runner = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, paired_by=owner,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    return owner, ws, agent, session, runner


def _bind(session, runner, key="hal-canopy-sweep-1"):
    return RunnerBinding.objects.create(session=session, runner=runner, session_key=key)


def test_an_agent_session_with_no_turn_is_interrupted(seeded, monkeypatch):
    """The whole point. No Turn owns this work — the only way to stop it is the
    terminal, addressed by session_key exactly as a menu answer is."""
    _owner, _ws, _agent, session, runner = seeded
    _bind(session, runner)

    published = []
    from apps.realtime import groups
    monkeypatch.setattr(groups, "publish", lambda group, msg: published.append((group, msg)))

    assert chat.interrupt_session(session) == "sent"

    [(group, msg)] = published
    assert group == groups.runner_group(runner.id)
    assert msg["type"] == "runner.session_interrupt"
    assert msg["session_key"] == "hal-canopy-sweep-1"
    assert msg["session_id"] == str(session.id)


def test_an_unbound_session_refuses_rather_than_raising(seeded):
    """A session canopy cannot reach a terminal for. Ordinary, not an error —
    mirrors answer_menu's refusal shape."""
    _owner, _ws, _agent, session, _runner = seeded
    assert chat.interrupt_session(session) == "unbound"


def test_a_binding_with_no_session_key_is_unbound(seeded):
    _owner, _ws, _agent, session, runner = seeded
    RunnerBinding.objects.create(session=session, runner=runner, session_key="")
    assert chat.interrupt_session(session) == "unbound"


def test_a_runner_that_is_gone_refuses(seeded):
    """Reachable, not available — but a runner whose heartbeat has gone stale can
    press nothing at all.

    Note `status` is left ONLINE on purpose: that field holds what the runner last
    self-REPORTED, and a box that died never gets to update it. `live_status`
    derives STALE from the heartbeat, which is the honest signal."""
    import datetime as dt

    _owner, _ws, _agent, session, runner = seeded
    Runner.objects.filter(pk=runner.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=2)
    )
    session.refresh_from_db()
    _bind(session, Runner.objects.get(pk=runner.pk))
    assert chat.interrupt_session(session) == "unavailable"


def test_a_paused_runner_still_presses_escape(seeded, monkeypatch):
    """Pause stops STARTING work, never finishing it, and an agent mid-turn is work
    already running — the same reasoning answer_menu documents."""
    _owner, _ws, _agent, session, runner = seeded
    Runner.objects.filter(pk=runner.pk).update(status=Runner.PAUSED,
                                               last_heartbeat_at=timezone.now())
    session.refresh_from_db()
    _bind(session, Runner.objects.get(pk=runner.pk))

    from apps.realtime import groups
    monkeypatch.setattr(groups, "publish", lambda group, msg: None)

    assert chat.interrupt_session(session) == "sent"


def test_a_chat_session_with_a_live_turn_is_not_double_interrupted(seeded, monkeypatch):
    """The turn's own cancel already interrupts this same terminal through the
    bridge. A second Escape could land after the agent has moved on to something
    else, so the session route is the FALLBACK, not an addition."""
    from apps.canopy_sessions.consumers import SessionConsumer

    _owner, _ws, _agent, session, runner = seeded
    _bind(session, runner)
    Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API,
                        idempotency_key="live-1", status=Turn.RUNNING, claimed_by=runner)

    interrupted = []
    monkeypatch.setattr(chat, "interrupt_session",
                        lambda s: interrupted.append(s) or "sent")

    consumer = SessionConsumer()
    consumer.session = session
    assert consumer._stop_session() is True
    assert interrupted == [], "the turn owned it — don't also fire at the terminal"


def test_stop_falls_through_to_the_session_when_no_turn_owns_it(seeded, monkeypatch):
    """The regression this whole change exists for: with no non-terminal turn,
    `_stop_session` used to return False — no interrupt, no broadcast, nothing."""
    from apps.canopy_sessions.consumers import SessionConsumer

    _owner, _ws, _agent, session, runner = seeded
    _bind(session, runner)

    interrupted = []
    monkeypatch.setattr(chat, "interrupt_session",
                        lambda s: interrupted.append(s) or "sent")

    consumer = SessionConsumer()
    consumer.session = session
    assert consumer._stop_session() is True
    assert interrupted == [session]


def test_stop_still_reports_nothing_happened_when_it_could_not_reach_anything(seeded, monkeypatch):
    """An unbound session must NOT flip every participant's Stop UI to
    "cancelled" — the M4 rule, preserved through the new fallback."""
    from apps.canopy_sessions.consumers import SessionConsumer

    _owner, _ws, _agent, session, _runner = seeded  # no binding at all

    consumer = SessionConsumer()
    consumer.session = session
    assert consumer._stop_session() is False
