"""The turn status actually reaching a chat client.

`test_turn_status.py` pins the state machine and `test_slack.py` pins Slack's
words for it; this pins the chain in between — enqueue fires, the session group
gets a frame, and the connect snapshot carries the same answer for a client
that was not watching when it changed.

The case driven throughout is a turn queued behind an OFFLINE runner, because
it is the one a client cannot derive for itself and the one that used to render
as an empty panel indistinguishable from a reply being written.
"""
from __future__ import annotations

import datetime as _dt

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import services as harness
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws)
    session = Session.objects.create(workspace=ws, created_by=user, agent=agent, title="t")
    return user, ws, agent, session


def _runner(name, *, pairer, ws, agent=None, online=True):
    beat = timezone.now() - (_dt.timedelta(0) if online else _dt.timedelta(hours=2))
    r = Runner.objects.create(name=name, kind=Runner.EMDASH, host=name, paired_by=pairer,
                              workspace_id=ws.pk, status=Runner.ONLINE,
                              last_heartbeat_at=beat, capabilities={"sessions": True})
    if agent is not None:
        RunnerAssignment.objects.create(agent=agent, runner=r, rank=0)
    return r


def _capture(monkeypatch):
    """`status_feed` binds `publish` at import, so patch it where it is USED.

    Patching `apps.realtime.groups.publish` would silently capture nothing here
    and leave the test passing while asserting on an empty list.
    """
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr("apps.canopy_sessions.status_feed.publish",
                        lambda g, m: sent.append((g, m)))
    return sent


def _statuses(sent, session):
    return [m["status"] for g, m in sent
            if g.endswith(session.id.hex) and m.get("type") == "session.turn_status"]


# -- the send moment -----------------------------------------------------------

def test_enqueueing_behind_an_offline_runner_says_so_immediately(monkeypatch):
    """The whole point. Nothing has touched the turn and nothing will, so no
    later event would ever report it — the status has to ride the enqueue."""
    user, ws, agent, session = _ctx()
    _runner("jj-mbp", pairer=user, ws=ws, agent=agent, online=False)
    sent = _capture(monkeypatch)

    harness.enqueue_turn(session=session, origin=Turn.ORIGIN_API,
                         idempotency_key="k1", prompt="hi")

    [status] = _statuses(sent, session)
    assert status["state"] == "waiting_runner"
    assert status["runners"] == ["jj-mbp"]
    # The one question a client actually asks of this.
    assert status["stuck"] is True
    assert status["settled"] is False


def test_enqueueing_with_a_live_runner_says_it_is_being_picked_up(monkeypatch):
    user, ws, agent, session = _ctx()
    _runner("jj-mbp", pairer=user, ws=ws, agent=agent, online=True)
    sent = _capture(monkeypatch)

    harness.enqueue_turn(session=session, origin=Turn.ORIGIN_API,
                         idempotency_key="k1", prompt="hi")

    [status] = _statuses(sent, session)
    assert status["state"] == "picking_up"
    # Nothing for a person to do — this resolves itself.
    assert status["stuck"] is False


def test_an_agent_with_no_runner_at_all_is_reported_as_unrouted(monkeypatch):
    """Distinct from 'offline' on purpose: waiting helps in one case and never
    helps in the other, and only the words can tell you which you are in."""
    user, ws, agent, session = _ctx()
    sent = _capture(monkeypatch)

    harness.enqueue_turn(session=session, origin=Turn.ORIGIN_API,
                         idempotency_key="k1", prompt="hi")

    [status] = _statuses(sent, session)
    assert status["state"] == "unrouted"
    assert status["stuck"] is True


def test_a_turn_with_no_session_publishes_nothing(monkeypatch):
    """An agent or project turn has no chat socket to push to."""
    user, ws, agent, session = _ctx()
    sent = _capture(monkeypatch)

    harness.enqueue_turn(agent=agent, origin=Turn.ORIGIN_API,
                         idempotency_key="k1", prompt="hi")

    assert _statuses(sent, session) == []


# -- the transitions after it -------------------------------------------------

def test_the_status_is_republished_as_the_turn_moves(monkeypatch):
    user, ws, agent, session = _ctx()
    runner = _runner("jj-mbp", pairer=user, ws=ws, agent=agent, online=True)
    turn, _ = harness.enqueue_turn(session=session, origin=Turn.ORIGIN_API,
                                   idempotency_key="k1", prompt="hi")
    turn.status, turn.claimed_by = Turn.RUNNING, runner
    turn.save(update_fields=["status", "claimed_by"])

    sent = _capture(monkeypatch)
    harness.append_events(turn, [{"kind": "status", "payload": {"state": "running"}}])

    assert _statuses(sent, session)[-1]["state"] == "working"


def test_a_non_status_row_does_not_republish(monkeypatch):
    """Every assistant token would otherwise re-derive the status, which means
    a fleet query per token."""
    user, ws, agent, session = _ctx()
    runner = _runner("jj-mbp", pairer=user, ws=ws, agent=agent, online=True)
    turn, _ = harness.enqueue_turn(session=session, origin=Turn.ORIGIN_API,
                                   idempotency_key="k1", prompt="hi")
    turn.status, turn.claimed_by = Turn.RUNNING, runner
    turn.save(update_fields=["status", "claimed_by"])

    sent = _capture(monkeypatch)
    harness.append_events(turn, [{"kind": "assistant", "payload": {"text": "hi"}}])

    assert _statuses(sent, session) == []


# -- the client that was not watching -----------------------------------------

def test_the_connect_snapshot_carries_the_same_answer():
    """A live-only frame reaches nobody who opened the page after it went quiet,
    which is the case that matters: you look BECAUSE it stopped."""
    from apps.canopy_sessions import serializers

    user, ws, agent, session = _ctx()
    _runner("jj-mbp", pairer=user, ws=ws, agent=agent, online=False)
    harness.enqueue_turn(session=session, origin=Turn.ORIGIN_API,
                         idempotency_key="k1", prompt="hi")

    state = serializers.session_state_dto(
        session=session, current_user_id=user.pk, participants=[],
        present_ids=[], draft=None, messages=[])

    assert state["turn_status"]["state"] == "waiting_runner"


def test_a_session_nobody_has_asked_anything_has_no_status():
    from apps.canopy_sessions import serializers

    user, ws, agent, session = _ctx()
    state = serializers.session_state_dto(
        session=session, current_user_id=user.pk, participants=[],
        present_ids=[], draft=None, messages=[])
    # Null rather than a synthetic "idle", so a client needs no second code path
    # and cannot mistake "never asked" for "finished".
    assert state["turn_status"] is None


# -- the frame survives the AG-UI projection ----------------------------------

def test_the_frame_reaches_an_ag_ui_client():
    """Both canopy's chat page and the embedded widget run `protocol: 'ag-ui'`,
    where an unmapped frame is silently DROPPED. Without this the status would
    be published correctly and seen by nobody."""
    from apps.canopy_sessions import agui

    events = agui.project(
        {"event": "session.turn_status", "data": {"status": {"state": "waiting_runner"}}},
        thread_id="t1", run_id="r1")

    assert events, "session.turn_status is dropped by the AG-UI projection"
    [event] = events
    assert event.name == "canopy.session.turn_status"
    assert event.value["status"]["state"] == "waiting_runner"


# -- the sweep, and what it must not cost -------------------------------------

def test_the_sweep_is_throttled_across_the_fleet(monkeypatch):
    """It rides `sessions_reported`, which EVERY runner fires every ~10s, and
    each run costs a `turn_reach` fleet query per unfinished session. Without
    a shared lock that is N scans per 10s for a signal — a laptop closing —
    that nobody needs answered faster than this."""
    from django.core.cache import cache

    from apps.canopy_sessions import status_feed

    cache.delete(status_feed.SWEEP_LOCK)
    user, ws, agent, session = _ctx()
    _runner("jj-mbp", pairer=user, ws=ws, agent=agent, online=False)
    harness.enqueue_turn(session=session, origin=Turn.ORIGIN_API,
                         idempotency_key="k1", prompt="hi")

    sent = _capture(monkeypatch)
    assert status_feed.sweep() == 1          # takes the lock
    assert status_feed.sweep() == 0          # second caller inside the window
    assert status_feed.sweep() == 0
    # Exactly one session's worth of frames, not three.
    assert len(_statuses(sent, session)) == 1


def test_force_bypasses_the_throttle_so_a_test_does_not_depend_on_lock_state():
    from django.core.cache import cache

    from apps.canopy_sessions import status_feed

    user, ws, agent, session = _ctx()
    _runner("jj-mbp", pairer=user, ws=ws, agent=agent, online=False)
    harness.enqueue_turn(session=session, origin=Turn.ORIGIN_API,
                         idempotency_key="k1", prompt="hi")
    cache.add(status_feed.SWEEP_LOCK, 1, timeout=60)   # somebody else holds it
    assert status_feed.sweep(force=True) == 1


def test_a_settled_turn_is_not_swept(monkeypatch):
    """A finished status cannot go stale, so sweeping it is pure cost."""
    from django.core.cache import cache

    from apps.canopy_sessions import status_feed

    cache.delete(status_feed.SWEEP_LOCK)
    user, ws, agent, session = _ctx()
    runner = _runner("jj-mbp", pairer=user, ws=ws, agent=agent, online=True)
    turn, _ = harness.enqueue_turn(session=session, origin=Turn.ORIGIN_API,
                                   idempotency_key="k1", prompt="hi")
    turn.status, turn.claimed_by = Turn.DONE, runner
    turn.save(update_fields=["status", "claimed_by"])

    sent = _capture(monkeypatch)
    assert status_feed.sweep(force=True) == 0
    assert _statuses(sent, session) == []
