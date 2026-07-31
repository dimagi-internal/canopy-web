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
        services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_API,
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
        services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_API,
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
        t, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_API,
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
        t, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_API,
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


# --- a SESSION turn over the socket ------------------------------------------
# Observed on the live cloud runner 2026-07-28: a session-targeted turn claimed
# over the WS ran and finished, but wrote ZERO durable Message rows and
# cold-started in a per-turn scratch dir. Both because `_serialize_turn`
# reimplemented a subset of TurnOut and lost the two fields a session turn is
# identified by. The REST claim path (full TurnOut) was always fine, which is
# why only the WS-preferring cloud runner hit it.
async def test_claim_over_ws_carries_a_session_turns_identity():
    user, ws, agent, runner = await database_sync_to_async(_setup)()

    @database_sync_to_async
    def _enqueue_session_turn():
        from apps.canopy_sessions import services as chat

        runner.capabilities = {**runner.capabilities, "sessions": True}
        runner.save(update_fields=["capabilities"])
        session = chat.create_session(workspace=ws, created_by=user, agent=agent)
        _msg, turn = chat.send_message(session=session, text="hello", user=user)
        return session, turn

    session, _turn = await _enqueue_session_turn()
    comm = await _connect(runner.id, user)
    await comm.connect()

    await comm.send_json_to({"action": "claim"})
    frame = await comm.receive_json_from(timeout=2)
    claimed = frame["turn"]
    assert claimed is not None, "a session-capable runner must be able to claim a session turn"

    # origin_ref.chat_session_id is THE invariant identity of a conversation. The
    # runner keys its stable per-session workdir on it (so `claude --resume`
    # works at all) and ships the transcript back under it (so the conversation
    # becomes durable rows). Without it both silently no-op.
    assert claimed["origin_ref"]["chat_session_id"] == str(session.id)
    # A session turn has agent_id NULL and project "" — its agent hangs off the
    # session. Deriving target from the columns alone reported "".
    assert claimed["target"] == "echo"
    assert claimed["agent_slug"] == "echo"
    assert claimed["workspace_slug"] == ws.slug
    await comm.disconnect()


async def test_ws_claim_agrees_with_the_rest_claim_payload():
    # The two claim paths must not disagree about a turn's identity — that
    # divergence IS this bug. Pin the overlap rather than the WS shape alone.
    user, ws, agent, runner = await database_sync_to_async(_setup)()

    @database_sync_to_async
    def _enqueue():
        services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_ACE_WEB,
                              idempotency_key="parity-1", prompt="p",
                              origin_ref={"marker": "keep-me"})

    await _enqueue()
    comm = await _connect(runner.id, user)
    await comm.connect()
    await comm.send_json_to({"action": "claim"})
    claimed = (await comm.receive_json_from(timeout=2))["turn"]

    @database_sync_to_async
    def _rest_shape(turn_id):
        from apps.harness.schemas import TurnOut

        return TurnOut.from_orm(Turn.objects.get(pk=turn_id)).dict()

    rest = await _rest_shape(claimed["id"])
    for field in ("target", "agent_slug", "project", "routing", "origin", "origin_ref",
                  "workspace_slug", "prompt"):
        assert claimed[field] == rest[field], f"{field} disagrees: {claimed[field]!r} vs {rest[field]!r}"
    await comm.disconnect()


# --- heartbeat provenance ----------------------------------------------------
# `services.heartbeat` assigns code_sha/code_branch/code_version/code_committed_at
# UNCONDITIONALLY, so a call that omits them clears them. This is the cloud
# runner's primary heartbeat (every 20s), so before spec 2026-07-30 anything it
# reported over REST was erased moments later and `code_sha` read empty forever —
# indistinguishable from a runner that never reported one.
async def test_ws_heartbeat_records_code_provenance():
    user, _ws, _a, runner = await database_sync_to_async(_setup)()
    comm = await _connect(runner.id, user)
    await comm.connect()
    await comm.send_json_to({
        "action": "heartbeat", "active_turn_ids": [],
        "code_sha": "abc123", "code_committed_at": 1753900000, "code_version": "9.9.9",
    })
    assert (await comm.receive_json_from(timeout=2))["type"] == "heartbeat.ack"

    fresh = await database_sync_to_async(Runner.objects.get)(pk=runner.id)
    assert fresh.code_sha == "abc123"
    assert fresh.code_committed_at == 1753900000
    assert fresh.code_version == "9.9.9"
    await comm.disconnect()


async def test_ws_heartbeat_does_not_erase_provenance_it_resent():
    # The regression itself: beat twice and the sha must still be there. A pass
    # here with a single beat would prove nothing — the erasure happened on the
    # NEXT one.
    user, _ws, _a, runner = await database_sync_to_async(_setup)()
    comm = await _connect(runner.id, user)
    await comm.connect()
    for _ in range(2):
        await comm.send_json_to({"action": "heartbeat", "active_turn_ids": [],
                                 "code_sha": "deadbee"})
        await comm.receive_json_from(timeout=2)

    fresh = await database_sync_to_async(Runner.objects.get)(pk=runner.id)
    assert fresh.code_sha == "deadbee"
    await comm.disconnect()


async def test_ws_heartbeat_survives_a_malformed_committed_at():
    # Provenance is decoration on a liveness call. A runner sending garbage must
    # lose its timestamp, never its heartbeat — losing the beat would take it
    # offline and stop it claiming.
    user, _ws, _a, runner = await database_sync_to_async(_setup)()
    comm = await _connect(runner.id, user)
    await comm.connect()
    await comm.send_json_to({"action": "heartbeat", "active_turn_ids": [],
                             "code_sha": "abc123", "code_committed_at": "not-a-number"})
    assert (await comm.receive_json_from(timeout=2))["type"] == "heartbeat.ack"

    fresh = await database_sync_to_async(Runner.objects.get)(pk=runner.id)
    assert fresh.status == Runner.ONLINE
    assert fresh.code_sha == "abc123"
    assert fresh.code_committed_at == 0
    await comm.disconnect()
