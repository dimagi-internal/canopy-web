"""SP1 Task 7 — SupervisorConsumer: auth gate, snapshot on connect, live deltas."""
from __future__ import annotations

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser, User

from apps.agents.models import Agent
from apps.harness.models import Runner
from apps.realtime.consumers import SupervisorConsumer
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def _user_ws_agent():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    return user, ws, agent


async def _connect(user):
    comm = WebsocketCommunicator(SupervisorConsumer.as_asgi(), "/ws/supervisor/")
    comm.scope["user"] = user
    return comm


async def test_anonymous_rejected():
    comm = await _connect(AnonymousUser())
    connected, code = await comm.connect()
    assert connected is False
    assert code == 4001


async def test_snapshot_on_connect():
    user, _ws, agent = await database_sync_to_async(_user_ws_agent)()
    comm = await _connect(user)
    connected, _ = await comm.connect()
    assert connected is True
    snap = await comm.receive_json_from(timeout=2)
    assert snap["type"] == "supervisor.snapshot"
    assert agent.slug in snap["waiting"]
    assert isinstance(snap["runners"], list)
    await comm.disconnect()


async def test_live_runner_delta():
    user, _ws, _agent = await database_sync_to_async(_user_ws_agent)()
    comm = await _connect(user)
    await comm.connect()
    await comm.receive_json_from(timeout=2)  # drain the snapshot

    def _make_runner():
        runner = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=user)
        runner.status = Runner.ONLINE
        runner.save(update_fields=["status"])
        return runner

    await database_sync_to_async(_make_runner)()
    frame = await comm.receive_json_from(timeout=2)
    assert frame["type"] == "supervisor.runner"
    assert frame["runner"]["name"] == "cloud-1"
    await comm.disconnect()
