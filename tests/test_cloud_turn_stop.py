"""A person's stop ends a turn running on a CLOUD runner.

2026-10-17, cloud-ec2-2: Ada stopped a huddle turn twice and closed its session;
the turn ran 23 more minutes. An agent turn names no session (agent XOR project
XOR chat_session), so `cancel_session_turns` — which looked only at
`chat_session` — found nothing; the fallback session interrupt went to a runner
that had no branch for it; and close archived the row over a live turn.

The turn IS tied to its session, by the key the runner stamped on it
(`stamp_turn_session`) — the key the RunnerBinding carries. Stop now finds it,
records cancel_requested, rings the runner, and re-delivers the stop on every
heartbeat reply until the runner finishes it CANCELLED.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import control as session_control
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness import services as harness
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

CLAUDE_SID = "f988d1eb-02be-44c9-b301-2f87232da936"


@pytest.fixture
def cloud():
    owner = User.objects.create_user("ada", "ada@dimagi-ai.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=owner)
    runner = Runner.objects.create(name="cloud-ec2-2", kind=Runner.CLOUD, owner=owner,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    session = Session.objects.create(agent=agent, workspace=ws,
                                     origin=Session.ORIGIN_RUNNER, title="huddle")
    # The cloud runner's binding: a Claude session id, and NO reported_at (only
    # the laptop's report loop writes that) — so close takes the unreported branch.
    RunnerBinding.objects.create(session=session, runner=runner, session_key=CLAUDE_SID)
    turn = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_API, prompt="/canopy:huddle --group-run 1",
        status=Turn.RUNNING, claimed_by=runner, claimed_at=timezone.now(),
        lease_expires_at=timezone.now() + dt.timedelta(seconds=900),
        session_id="cloud-fa383225", session_key=CLAUDE_SID, idempotency_key="huddle-1",
    )
    session.refresh_from_db()
    return owner, agent, runner, session, turn


@pytest.fixture
def published(monkeypatch):
    from apps.realtime import groups

    sent = []
    monkeypatch.setattr(groups, "publish", lambda group, msg: sent.append((group, msg)))
    return sent


def _cancel_frames(published, runner):
    from apps.realtime import groups

    return [m for g, m in published
            if g == groups.runner_group(runner.id) and m["type"] == "runner.cancel"]


def test_stop_reaches_the_running_cloud_turn_and_rings_its_runner(cloud, published):
    _owner, _agent, runner, session, turn = cloud

    assert session_control.stop_session(session, by="Ada") == "turns"

    assert turn.events.filter(kind="cancel_requested").exists()
    assert _cancel_frames(published, runner) == [{"type": "runner.cancel", "turn_id": str(turn.id)}]


def test_rest_stop_answers_cancelled_for_a_cloud_turn(cloud, published):
    owner, _agent, _runner, session, turn = cloud
    c = Client()
    c.force_login(owner)
    r = c.post(f"/api/canopy-sessions/{session.id}/stop", content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["cancelled"] is True and r.json()["route"] == "turns"
    assert turn.events.filter(kind="cancel_requested").exists()


def test_the_heartbeat_reply_redelivers_the_stop_until_the_turn_ends(cloud, published):
    """The frame is a doorbell; a box on REST or mid-reconnect never hears it.
    Every heartbeat reply carries the stop, REST and WS alike."""
    owner, _agent, runner, session, turn = cloud
    session_control.stop_session(session)

    beat = harness.heartbeat(runner, active_turn_ids=[str(turn.id)])
    assert beat.cancel_turn_ids == [str(turn.id)]

    c = Client()
    c.force_login(owner)
    r = c.post(f"/api/harness/runners/{runner.id}/heartbeat",
               {"active_turn_ids": [str(turn.id)]}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["cancel_turn_ids"] == [str(turn.id)]

    # The runner finishes it CANCELLED; the next beat owes nothing.
    harness.finish_turn(turn, status=Turn.CANCELLED, result_note="stopped on request")
    assert harness.heartbeat(runner, active_turn_ids=[str(turn.id)]).cancel_turn_ids == []


def test_an_unstopped_turn_is_owed_nothing(cloud):
    _owner, _agent, runner, _session, turn = cloud
    assert harness.heartbeat(runner, active_turn_ids=[str(turn.id)]).cancel_turn_ids == []


def test_another_runners_turn_is_never_named(cloud, published):
    owner, _agent, runner, session, turn = cloud
    session_control.stop_session(session)
    other = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, owner=owner,
                                  status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    assert harness.heartbeat(other, active_turn_ids=[str(turn.id)]).cancel_turn_ids == []


def test_close_on_a_cloud_session_stops_its_running_turn(cloud, published):
    _owner, _agent, runner, session, turn = cloud

    assert session_control.close_session(session=session) == "closed"

    assert turn.events.filter(kind="cancel_requested").exists()
    assert _cancel_frames(published, runner)
    session.refresh_from_db()
    assert session.status == Session.ARCHIVED


def test_a_stop_the_runner_never_honours_lets_the_lease_lapse(cloud, published):
    """The runner owns the lease — but not the right to hold a stopped turn forever
    by heartbeating it. Past the grace it is not renewed and the sweep closes it
    CANCELLED, freeing the agent's executing slot."""
    from apps.harness.claim import sweep_expired_leases

    _owner, _agent, runner, session, turn = cloud
    session_control.stop_session(session)
    old = timezone.now() - dt.timedelta(seconds=harness.CANCEL_GRACE_SECONDS + 5)
    turn.events.filter(kind="cancel_requested").update(ts=old)
    Turn.objects.filter(pk=turn.pk).update(lease_expires_at=timezone.now() - dt.timedelta(seconds=1))

    harness.heartbeat(runner, active_turn_ids=[str(turn.id)])
    turn.refresh_from_db()
    assert turn.lease_expires_at < timezone.now()  # not renewed

    sweep_expired_leases()
    turn.refresh_from_db()
    assert turn.status == Turn.CANCELLED


def test_a_fresh_stop_keeps_the_lease_renewed(cloud, published):
    _owner, _agent, runner, session, turn = cloud
    session_control.stop_session(session)
    Turn.objects.filter(pk=turn.pk).update(lease_expires_at=timezone.now())
    harness.heartbeat(runner, active_turn_ids=[str(turn.id)])
    turn.refresh_from_db()
    assert turn.lease_expires_at > timezone.now() + dt.timedelta(seconds=600)


def test_a_same_keyed_turn_of_another_agent_is_left_alone(cloud, published):
    """A laptop key is an emdash task name, unique per project only — so the match
    is scoped to the session's own agent and the binding's runner."""
    owner, _agent, runner, session, turn = cloud
    other_agent = Agent.objects.create(slug="hal", name="Hal", workspace=session.workspace,
                                       owner=owner)
    stranger = Turn.objects.create(
        agent=other_agent, origin=Turn.ORIGIN_API, prompt="x", status=Turn.RUNNING,
        claimed_by=runner, session_key=CLAUDE_SID, idempotency_key="hal-1",
    )
    session_control.stop_session(session)
    assert turn.events.filter(kind="cancel_requested").exists()
    assert not stranger.events.filter(kind="cancel_requested").exists()


def test_the_ws_heartbeat_ack_carries_the_stop(cloud, published):
    from apps.realtime.consumers import RunnerConsumer

    _owner, _agent, runner, session, turn = cloud
    session_control.stop_session(session)
    consumer = RunnerConsumer()
    consumer._runner_pk = runner.pk
    refresh, cancel_ids, _pause = consumer._heartbeat.__wrapped__(
        consumer, {"active_turn_ids": [str(turn.id)]})
    assert cancel_ids == [str(turn.id)]
