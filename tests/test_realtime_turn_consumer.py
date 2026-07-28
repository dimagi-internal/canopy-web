"""SP1 Task 6 — TurnConsumer: auth gates, cursor replay, live tail.

Unit-tests the consumer in isolation: we bypass the router+auth middleware and
set scope['user'] / url_route / query_string directly on the communicator, so
these tests cover the consumer's own logic (not the handshake, which Task 5 owns).
"""
from __future__ import annotations

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser, User

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Turn
from apps.realtime.consumers import TurnConsumer
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def _member_turn():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="k1")
    return user, turn


async def _connect(turn, user, after=None):
    path = f"/ws/turns/{turn.id}/"
    if after is not None:
        path += f"?after={after}"
    comm = WebsocketCommunicator(TurnConsumer.as_asgi(), path)
    comm.scope["user"] = user
    comm.scope["url_route"] = {"kwargs": {"turn_id": str(turn.id)}}
    return comm


async def test_anonymous_rejected():
    _user, turn = await database_sync_to_async(_member_turn)()
    comm = await _connect(turn, AnonymousUser())
    connected, code = await comm.connect()
    assert connected is False
    assert code == 4001


async def test_non_member_forbidden():
    _user, turn = await database_sync_to_async(_member_turn)()
    outsider = await database_sync_to_async(User.objects.create_user)("no", "no@dimagi.com", "pw")
    comm = await _connect(turn, outsider)
    connected, code = await comm.connect()
    assert connected is False
    assert code == 4003


async def test_member_gets_replay_then_live_tail():
    user, turn = await database_sync_to_async(_member_turn)()
    # A pre-existing event should be replayed on connect.
    await database_sync_to_async(services.append_events)(
        turn, [{"kind": "assistant", "payload": {"text": "old"}}]
    )
    comm = await _connect(turn, user)
    connected, _ = await comm.connect()
    assert connected is True

    replayed = await comm.receive_json_from(timeout=2)
    assert replayed["event"]["seq"] == 1
    assert replayed["event"]["payload"]["text"] == "old"

    # A new append after connect should arrive live over the group.
    await database_sync_to_async(services.append_events)(
        turn, [{"kind": "assistant", "payload": {"text": "new"}}]
    )
    live = await comm.receive_json_from(timeout=2)
    assert live["event"]["seq"] == 2
    assert live["event"]["payload"]["text"] == "new"
    await comm.disconnect()


async def test_replay_pages_beyond_500_events():
    # A turn with more than one replay page (500) must deliver ALL unseen events
    # on connect — no silent truncation (the SP2 chat path routinely exceeds it).
    user, turn = await database_sync_to_async(_member_turn)()
    await database_sync_to_async(services.append_events)(
        turn, [{"kind": "assistant", "payload": {"i": i}} for i in range(600)]
    )
    comm = await _connect(turn, user)
    connected, _ = await comm.connect()
    assert connected is True
    seqs = []
    for _ in range(600):
        frame = await comm.receive_json_from(timeout=3)
        seqs.append(frame["event"]["seq"])
    assert seqs == list(range(1, 601))  # every event, in order, no gap
    await comm.disconnect()


async def test_cursor_replay_skips_seen_events():
    user, turn = await database_sync_to_async(_member_turn)()
    await database_sync_to_async(services.append_events)(
        turn, [{"kind": "assistant", "payload": {}}, {"kind": "assistant", "payload": {}}]
    )
    comm = await _connect(turn, user, after=1)  # already saw seq 1
    connected, _ = await comm.connect()
    assert connected is True
    frame = await comm.receive_json_from(timeout=2)
    assert frame["event"]["seq"] == 2  # only the unseen one is replayed
    await comm.disconnect()
