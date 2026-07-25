"""RC1 — RunnerConsumer: PAT-authed control channel, wake-on-enqueue, claim over WS.

The runner keeps a persistent socket; enqueue publishes a wake to its workspace's
runnable group (the runner claims on it), and claim/heartbeat frames call the same
harness services the REST routes do. Postgres stays the source of truth."""
from __future__ import annotations

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser, User
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.realtime.consumers import RunnerConsumer
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def _setup():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    runner = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, paired_by=user,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"agents": ["echo"]},
    )
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    return user, ws, agent, runner


async def _connect(runner_id, user):
    comm = WebsocketCommunicator(RunnerConsumer.as_asgi(), f"/ws/runner/{runner_id}/")
    comm.scope["user"] = user
    comm.scope["url_route"] = {"kwargs": {"runner_id": str(runner_id)}}
    return comm


# --- auth / ownership --------------------------------------------------------
async def test_anonymous_rejected():
    user, _ws, _a, runner = await database_sync_to_async(_setup)()
    comm = await _connect(runner.id, AnonymousUser())
    connected, code = await comm.connect()
    assert connected is False and code == 4001


async def test_non_owner_rejected():
    user, _ws, _a, runner = await database_sync_to_async(_setup)()
    other = await database_sync_to_async(User.objects.create_user)("x", "x@dimagi.com", "pw")
    comm = await _connect(runner.id, other)
    connected, code = await comm.connect()
    assert connected is False and code == 4003


async def test_owner_connects():
    user, _ws, _a, runner = await database_sync_to_async(_setup)()
    comm = await _connect(runner.id, user)
    connected, _ = await comm.connect()
    assert connected is True
    await comm.disconnect()


# --- wake on enqueue ---------------------------------------------------------
async def test_enqueue_wakes_the_runner():
    user, ws, agent, runner = await database_sync_to_async(_setup)()
    comm = await _connect(runner.id, user)
    await comm.connect()

    @database_sync_to_async
    def _enqueue():
        services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_MANUAL,
                              idempotency_key="w1", prompt="hi")

    await _enqueue()
    frame = await comm.receive_json_from(timeout=2)
    assert frame == {"type": "wake"}
    await comm.disconnect()


# --- claim over the socket ---------------------------------------------------
async def test_claim_over_ws_returns_a_turn():
    user, ws, agent, runner = await database_sync_to_async(_setup)()

    @database_sync_to_async
    def _enqueue():
        services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_MANUAL,
                              idempotency_key="c1", prompt="do the thing")

    await _enqueue()  # queued before connect — so no wake reaches this socket
    comm = await _connect(runner.id, user)
    await comm.connect()

    await comm.send_json_to({"action": "claim"})
    frame = await comm.receive_json_from(timeout=2)
    assert frame["type"] == "claim.result"
    assert frame["turn"]["target"] == "echo"
    assert frame["turn"]["prompt"] == "do the thing"
    await comm.disconnect()


async def test_claim_over_ws_empty_when_nothing_queued():
    user, _ws, _a, runner = await database_sync_to_async(_setup)()
    comm = await _connect(runner.id, user)
    await comm.connect()
    await comm.send_json_to({"action": "claim"})
    frame = await comm.receive_json_from(timeout=2)
    assert frame == {"type": "claim.result", "turn": None}
    await comm.disconnect()


# --- run a whole turn over the socket (start/event/finish) -------------------
async def test_run_turn_end_to_end_over_ws():
    user, ws, agent, runner = await database_sync_to_async(_setup)()

    @database_sync_to_async
    def _enqueue():
        t, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_MANUAL,
                                     idempotency_key="run1", prompt="go")
        return t

    await _enqueue()
    comm = await _connect(runner.id, user)
    await comm.connect()

    await comm.send_json_to({"action": "claim"})
    claimed = await comm.receive_json_from(timeout=2)
    tid = claimed["turn"]["id"]

    await comm.send_json_to({"action": "start", "turn_id": tid})
    assert (await comm.receive_json_from(timeout=2)) == {"type": "start.ack", "ok": True}

    await comm.send_json_to({"action": "event", "turn_id": tid,
                             "events": [{"kind": "assistant", "payload": {"text": "hello"}}]})
    ev = await comm.receive_json_from(timeout=2)
    assert ev["type"] == "event.ack" and ev["count"] >= 1

    await comm.send_json_to({"action": "finish", "turn_id": tid,
                             "status": "done", "result_note": "ok"})
    assert (await comm.receive_json_from(timeout=2)) == {"type": "finish.ack", "ok": True}

    @database_sync_to_async
    def _final():
        t = Turn.objects.get(pk=tid)
        return t.status, t.events.filter(kind="assistant").count()

    status, n_assistant = await _final()
    assert status == Turn.DONE and n_assistant >= 1
    await comm.disconnect()


async def test_cannot_touch_a_turn_it_did_not_claim():
    user, ws, agent, runner = await database_sync_to_async(_setup)()

    @database_sync_to_async
    def _foreign_turn():
        t, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_MANUAL,
                                     idempotency_key="foreign", prompt="x")
        return str(t.id)

    other_tid = await _foreign_turn()  # queued, not claimed by this runner
    comm = await _connect(runner.id, user)
    await comm.connect()
    await comm.send_json_to({"action": "finish", "turn_id": other_tid, "status": "done"})
    assert (await comm.receive_json_from(timeout=2)) == {"type": "finish.ack", "ok": False}
    await comm.disconnect()


# --- interjection reaches the runner -----------------------------------------
async def test_interject_frame_reaches_the_runner():
    from channels.layers import get_channel_layer

    from apps.realtime import groups

    user, ws, agent, runner = await database_sync_to_async(_setup)()
    comm = await _connect(runner.id, user)
    await comm.connect()

    layer = get_channel_layer()
    await layer.group_send(groups.runner_group(runner.id), {
        "type": "runner.interject", "turn_id": "t-123", "session_id": "s-1",
        "message": "wait, change the plan",
    })
    frame = await comm.receive_json_from(timeout=2)
    assert frame == {"type": "interject", "turn_id": "t-123", "session_id": "s-1",
                     "message": "wait, change the plan"}
    await comm.disconnect()


async def test_cancel_frame_reaches_the_runner():
    from channels.layers import get_channel_layer

    from apps.realtime import groups

    user, ws, agent, runner = await database_sync_to_async(_setup)()
    comm = await _connect(runner.id, user)
    await comm.connect()

    layer = get_channel_layer()
    await layer.group_send(groups.runner_group(runner.id), {
        "type": "runner.cancel", "turn_id": "t-456",
    })
    frame = await comm.receive_json_from(timeout=2)
    assert frame == {"type": "cancel", "turn_id": "t-456"}
    await comm.disconnect()


async def test_send_message_interjects_the_running_runner():
    from apps.canopy_sessions.models import Session
    from apps.canopy_sessions.services import send_message

    @database_sync_to_async
    def _running():
        u = User.objects.create_user("jj2", "jj2@dimagi.com", "pw")
        w = Workspace.objects.create(slug="c2", display_name="C2", created_by=u)
        WorkspaceMembership.objects.create(user=u, workspace=w, role=WorkspaceMembership.OWNER)
        r = Runner.objects.create(name="cloud-s", kind=Runner.CLOUD, paired_by=u,
                                  status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                  capabilities={"sessions": True})
        s = Session.objects.create(workspace=w, created_by=u)
        run = Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_API,
                                  idempotency_key="running-1", status=Turn.RUNNING,
                                  claimed_by=r, prompt="first")
        return u, r, s, run

    user, runner, session, running = await _running()
    comm = await _connect(runner.id, user)
    await comm.connect()

    @database_sync_to_async
    def _send():
        send_message(session=session, text="actually, stop and do X", user=user, client_id="c9")

    await _send()
    # The send also queues a new turn (→ a wake on the runnable group the runner
    # also joined), so drain a few frames and find the interject.
    interject = None
    for _ in range(3):
        f = await comm.receive_json_from(timeout=2)
        if f.get("type") == "interject":
            interject = f
            break
    assert interject is not None
    assert interject["message"] == "actually, stop and do X"
    assert interject["turn_id"] == str(running.id)
    await comm.disconnect()
