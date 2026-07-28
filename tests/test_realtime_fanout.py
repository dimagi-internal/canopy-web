"""SP1 Task 4 — the three fan-out receivers put frames on the right groups.

transaction=True so on_commit callbacks actually fire (pytest-django's default
marker rolls the wrapping atomic back, discarding on_commit). All channel-layer
ops go through async_to_sync in the test's own thread, which asgiref pins to a
single cached event loop — so the InMemoryChannelLayer queues stay consistent
across group_add / group_send / receive.
"""
from __future__ import annotations

import pytest
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, Turn
from apps.push.models import AgentWaitingSnapshot
from apps.realtime import groups
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def _fixtures():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    return user, ws, agent


def test_turn_event_fanout():
    _user, _ws, agent = _fixtures()
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="k1")
    layer = get_channel_layer()
    async_to_sync(layer.group_add)(groups.turn_group(turn.id), "turn-chan")

    services.append_events(turn, [{"kind": "assistant", "payload": {"text": "hi"}}])

    msg = async_to_sync(layer.receive)("turn-chan")
    assert msg["type"] == "turn.event"
    assert msg["event"]["seq"] == 1
    assert msg["event"]["kind"] == "assistant"


def test_runner_fanout_to_pairer():
    user, _ws, _agent = _fixtures()
    runner = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, paired_by=user)
    layer = get_channel_layer()
    async_to_sync(layer.group_add)(groups.supervisor_user_group(user.id), "sup-chan")

    # A heartbeat-style save re-fires post_save -> on_commit publish.
    runner.status = Runner.ONLINE
    runner.save(update_fields=["status"])

    msg = async_to_sync(layer.receive)("sup-chan")
    assert msg["type"] == "supervisor.runner"
    assert msg["runner"]["id"] == str(runner.id)
    assert msg["runner"]["name"] == "cloud-1"


def test_runner_with_no_pairer_does_not_fan_out():
    # No subscriber assertion is fragile; instead assert the guard directly by
    # saving a pairer-less runner and confirming nothing lands for a bystander.
    _user, _ws, _agent = _fixtures()
    runner = Runner.objects.create(name="orphan", kind=Runner.CLOUD)
    layer = get_channel_layer()
    async_to_sync(layer.group_add)(groups.supervisor_user_group(999999), "nobody-chan")
    runner.status = Runner.ONLINE
    runner.save(update_fields=["status"])
    # Nothing should be queued for an unrelated group; receive would block, so
    # assert emptiness via the layer's per-channel queue.
    import asyncio

    async def _empty():
        try:
            await asyncio.wait_for(layer.receive("nobody-chan"), timeout=0.2)
            return False
        except (asyncio.TimeoutError, TimeoutError):
            return True

    assert async_to_sync(_empty)() is True


def test_waiting_fanout_to_members():
    user, _ws, agent = _fixtures()
    # Create the snapshot BEFORE subscribing so its create-post_save goes nowhere.
    snap, _ = AgentWaitingSnapshot.objects.get_or_create(agent=agent)
    layer = get_channel_layer()
    async_to_sync(layer.group_add)(groups.supervisor_user_group(user.id), "sup-chan")

    snap.waiting_count = 3
    snap.save(update_fields=["waiting_count", "updated_at"])

    msg = async_to_sync(layer.receive)("sup-chan")
    assert msg["type"] == "supervisor.waiting"
    assert msg["agent"] == agent.slug
    assert msg["waiting_count"] == 3


def test_sessions_fanout_to_pairer():
    """A runner's session report pushes the owner's visible sessions to their
    supervisor group — the WS broadcast that replaces per-client polling."""
    from django.utils import timezone

    from apps.harness.schemas import ReportedSessionIn

    user, ws, _agent = _fixtures()
    runner = Runner.objects.create(
        name="laptop", kind=Runner.EMDASH, paired_by=user, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    layer = get_channel_layer()
    async_to_sync(layer.group_add)(groups.supervisor_user_group(user.id), "sup-chan")

    services.replace_reported_sessions(
        runner, ws,
        [ReportedSessionIn(emdash_task="echo-1", project="echo", status="in_progress",
                           recent_messages=[{"role": "user", "text": "hi"}])],
    )

    msg = async_to_sync(layer.receive)("sup-chan")
    assert msg["type"] == "supervisor.sessions"
    assert "echo-1" in [s["emdash_task"] for s in msg["sessions"]]
