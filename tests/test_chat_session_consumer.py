"""SP3 Task 3 — the per-session multiplayer SessionConsumer.

Two sockets on one session: a draft edit by one is broadcast to the other, and a
commit sends + streams the assistant response to everyone (the stub executes).
"""
from __future__ import annotations

import pytest
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser, User
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import attach as attach_registry
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.consumers import SessionConsumer
from apps.canopy_sessions.models import Message, SessionParticipant
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def _seed():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    teammate = User.objects.create_user("t", "t@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=teammate, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=owner)
    session = chat.create_session(workspace=ws, created_by=owner, agent=agent)
    return owner, teammate, session


async def _connect(session, user):
    comm = WebsocketCommunicator(SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{session.id}/")
    comm.scope["user"] = user
    comm.scope["url_route"] = {"kwargs": {"session_id": str(session.id)}}
    return comm


async def _recv_match(comm, pred, tries=14):
    for _ in range(tries):
        frame = await comm.receive_json_from(timeout=2)
        if pred(frame):
            return frame
    raise AssertionError("expected frame not received")


async def test_anonymous_rejected():
    _o, _t, session = await database_sync_to_async(_seed)()
    comm = await _connect(session, AnonymousUser())
    connected, code = await comm.connect()
    assert connected is False
    assert code == 4001


async def test_non_member_rejected():
    _o, _t, session = await database_sync_to_async(_seed)()
    outsider = await database_sync_to_async(User.objects.create_user)("no", "no@dimagi.com", "pw")
    comm = await _connect(session, outsider)
    connected, code = await comm.connect()
    assert connected is False
    assert code == 4003


async def test_member_gets_session_snapshot():
    owner, _t, session = await database_sync_to_async(_seed)()
    comm = await _connect(session, owner)
    connected, _ = await comm.connect()
    assert connected is True
    snap = await _recv_match(comm, lambda f: f.get("event") == "session.state")
    roles = {p["role"] for p in snap["data"]["participants"]}
    assert "owner" in roles
    await comm.disconnect()


async def test_snapshot_is_canonical():
    owner, _t, session = await database_sync_to_async(_seed)()
    comm = await _connect(session, owner)
    assert (await comm.connect())[0]
    snap = await _recv_match(comm, lambda f: f.get("event") == "session.state")
    data = snap["data"]
    assert set(data) >= {"messages", "active_draft", "participants",
                         "presence_user_ids", "current_user_id"}
    assert data["current_user_id"] == owner.id
    assert owner.id in data["presence_user_ids"]
    # participants carry full identity, not just {user_id, role}
    assert data["participants"][0]["email"] == owner.email
    await comm.disconnect()


async def test_draft_update_broadcasts_to_other_socket():
    owner, teammate, session = await database_sync_to_async(_seed)()
    a = await _connect(session, owner)
    assert (await a.connect())[0]
    b = await _connect(session, teammate)
    assert (await b.connect())[0]

    await a.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "hello team"}})
    frame = await _recv_match(b, lambda f: f.get("event") == "draft.updated")
    assert frame["data"]["body"] == "hello team"
    assert frame["data"]["last_editor"] == owner.id
    # canonical draft.updated is the full DraftSerializer shape
    assert {"id", "slot", "status", "last_edit_at"} <= set(frame["data"])
    await a.disconnect()
    await b.disconnect()


async def test_commit_sends_and_streams_assistant_to_all():
    owner, teammate, session = await database_sync_to_async(_seed)()
    a = await _connect(session, owner)
    assert (await a.connect())[0]
    b = await _connect(session, teammate)
    assert (await b.connect())[0]

    await a.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "do it"}})
    await _recv_match(a, lambda f: f.get("event") == "draft.updated")
    await a.send_json_to({"action": "chat.send", "data": {}})

    # Both sockets see the streamed assistant reply as canonical stream frames.
    is_complete = lambda f: f.get("event") == "chat.stream_complete"
    assert await _recv_match(a, is_complete)
    assert await _recv_match(b, is_complete)

    # And the durable transcript has the user + assistant messages.
    roles = await database_sync_to_async(
        lambda: [m.role for m in session.messages.order_by("turn_index")]
    )()
    assert roles == ["user", "assistant"]
    await a.disconnect()
    await b.disconnect()


async def test_commit_survives_concurrent_running_turn():
    # A turn already executing on this session holds one_executing_turn_per_session.
    # A commit must NOT crash the socket, and the cleared-draft frame must arrive.
    owner, _t, session = await database_sync_to_async(_seed)()
    await database_sync_to_async(
        lambda: Turn.objects.create(
            chat_session=session, origin=Turn.ORIGIN_API, idempotency_key="pre", status=Turn.RUNNING
        )
    )()
    a = await _connect(session, owner)
    assert (await a.connect())[0]
    await a.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "hi"}})
    await _recv_match(a, lambda f: f.get("event") == "draft.updated")
    await a.send_json_to({"action": "chat.send", "data": {}})
    # cleared-draft frame arrives (socket did not tear down on the IntegrityError)
    cleared = await _recv_match(a, lambda f: f.get("event") == "draft.updated" and f["data"]["body"] == "")
    assert cleared["data"]["body"] == ""
    # socket is still responsive
    await a.send_json_to({"action": "presence.heartbeat"})
    await a.disconnect()


async def test_viewer_cannot_edit():
    owner, teammate, session = await database_sync_to_async(_seed)()
    await database_sync_to_async(
        lambda: SessionParticipant.objects.update_or_create(
            session=session, user=teammate, defaults={"role": SessionParticipant.VIEWER}
        )
    )()
    b = await _connect(session, teammate)
    assert (await b.connect())[0]
    await _recv_match(b, lambda f: f.get("event") == "session.state")
    await b.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "nope"}})
    err = await _recv_match(b, lambda f: f.get("event") == "session.error")
    assert err["data"]["code"] == "forbidden"
    await b.disconnect()


async def test_version_conflict_uses_session_error():
    owner, _t, session = await database_sync_to_async(_seed)()
    comm = await _connect(session, owner)
    assert (await comm.connect())[0]
    await _recv_match(comm, lambda f: f.get("event") == "session.state")
    await comm.send_json_to({"action": "draft.update", "data": {"version": 99, "body": "x"}})
    err = await _recv_match(comm, lambda f: f.get("event") == "session.error")
    assert err["data"]["code"] == "draft_version_mismatch"
    assert "current_version" in err["data"]["detail"]
    await comm.disconnect()


async def test_send_broadcasts_draft_committed():
    owner, _t, session = await database_sync_to_async(_seed)()
    comm = await _connect(session, owner)
    assert (await comm.connect())[0]
    await _recv_match(comm, lambda f: f.get("event") == "session.state")
    await comm.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "ping"}})
    await _recv_match(comm, lambda f: f.get("event") == "draft.updated")
    await comm.send_json_to({"action": "chat.send", "data": {}})
    committed = await _recv_match(comm, lambda f: f.get("event") == "draft.committed")
    assert "user_message_id" in committed["data"]
    await comm.disconnect()


async def test_snapshot_ships_tail_not_head():
    owner, _t, session = await database_sync_to_async(_seed)()

    @database_sync_to_async
    def _fill():
        for i in range(chat.SESSION_TAIL_DEFAULT + 15):
            Message.objects.create(
                session=session, turn_index=i, role=Message.USER, plaintext=f"m{i}",
            )

    await _fill()
    comm = await _connect(session, owner)
    connected, _ = await comm.connect()
    assert connected is True
    snap = await _recv_match(comm, lambda f: f.get("event") == "session.state")
    msgs = snap["data"]["messages"]
    assert len(msgs) == chat.SESSION_TAIL_DEFAULT
    # The LAST N, chronological — i.e. the tail, not messages[:200] (the head).
    # Derived from the constant so a retuned tail size can't silently stale it.
    total = chat.SESSION_TAIL_DEFAULT + 15
    assert [m["turn_index"] for m in msgs] == list(
        range(total - chat.SESSION_TAIL_DEFAULT, total)
    )
    await comm.disconnect()


async def test_assistant_event_streams_canonical_frames():
    owner, _t, session = await database_sync_to_async(_seed)()
    comm = await _connect(session, owner)
    assert (await comm.connect())[0]
    await _recv_match(comm, lambda f: f.get("event") == "session.state")
    await comm.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "hi"}})
    await _recv_match(comm, lambda f: f.get("event") == "draft.updated")
    await comm.send_json_to({"action": "chat.send", "data": {}})
    await _recv_match(comm, lambda f: f.get("event") == "chat.stream_start")
    done = await _recv_match(comm, lambda f: f.get("event") == "chat.stream_complete")
    assert "plaintext" in done["data"]
    await comm.disconnect()


async def test_heartbeat_renews_attach_count(monkeypatch):
    owner, _t, session = await database_sync_to_async(_seed)()
    renewed = []
    monkeypatch.setattr(attach_registry, "renew", lambda sid: renewed.append(sid) or 1)
    comm = await _connect(session, owner)
    connected, _ = await comm.connect()
    assert connected is True
    await comm.receive_json_from()  # drain the session.state snapshot
    await comm.send_json_to({"action": "presence.heartbeat", "data": {}})
    # Give the consumer a tick to process, then assert renew fired for this session.
    import asyncio
    await asyncio.sleep(0.05)
    assert str(session.id) in [str(s) for s in renewed]
    await comm.disconnect()


async def test_stop_broadcasts_to_whole_group_and_cancels_queued_turn():
    # chat.stop must reach EVERY participant's Stop UI, not just the sender's —
    # and it must actually cancel the queued turn via harness_services.cancel_turn
    # (Task 5), not just ack the sender.
    owner, teammate, session = await database_sync_to_async(_seed)()
    turn = await database_sync_to_async(
        lambda: Turn.objects.create(
            chat_session=session, origin=Turn.ORIGIN_API, idempotency_key="q1", status=Turn.QUEUED
        )
    )()
    a = await _connect(session, owner)
    assert (await a.connect())[0]
    b = await _connect(session, teammate)
    assert (await b.connect())[0]

    await a.send_json_to({"action": "chat.stop", "data": {"message_id": "m1"}})
    frame_a = await _recv_match(a, lambda f: f.get("event") == "chat.stream_cancelled")
    frame_b = await _recv_match(b, lambda f: f.get("event") == "chat.stream_cancelled")
    assert frame_a["data"] == {"message_id": "m1", "partial_len": 0}
    assert frame_b["data"] == {"message_id": "m1", "partial_len": 0}

    await database_sync_to_async(turn.refresh_from_db)()
    assert turn.status == Turn.CANCELLED
    await a.disconnect()
    await b.disconnect()


async def test_stop_cancels_running_and_queued_turns_both():
    # I1: a mid-reply send queues turn B behind still-running turn A — chat.stop
    # must reach BOTH, not just the newest. B (queued) finishes CANCELLED, A
    # (running) gets a cancel_requested ledger event.
    owner, _teammate, session = await database_sync_to_async(_seed)()
    turn_a, turn_b = await database_sync_to_async(lambda: (
        Turn.objects.create(
            chat_session=session, origin=Turn.ORIGIN_API, idempotency_key="a1", status=Turn.RUNNING,
        ),
        Turn.objects.create(
            chat_session=session, origin=Turn.ORIGIN_API, idempotency_key="b1", status=Turn.QUEUED,
        ),
    ))()

    a = await _connect(session, owner)
    assert (await a.connect())[0]
    await a.send_json_to({"action": "chat.stop", "data": {"message_id": "m1"}})
    await _recv_match(a, lambda f: f.get("event") == "chat.stream_cancelled")

    await database_sync_to_async(turn_a.refresh_from_db)()
    await database_sync_to_async(turn_b.refresh_from_db)()
    assert turn_a.status == Turn.RUNNING  # untouched — runner owns the lease
    has_cancel_requested = await database_sync_to_async(
        lambda: turn_a.events.filter(kind="cancel_requested").exists()
    )()
    assert has_cancel_requested
    assert turn_b.status == Turn.CANCELLED
    await a.disconnect()


async def test_stop_with_nothing_to_cancel_does_not_broadcast():
    # M4: _chat_stop must not fan out chat.stream_cancelled when there was
    # nothing non-terminal to cancel — otherwise every participant's Stop UI
    # flips to "cancelled" for no reason.
    owner, _teammate, session = await database_sync_to_async(_seed)()
    a = await _connect(session, owner)
    assert (await a.connect())[0]
    await a.receive_json_from()  # drain the session.state snapshot
    await a.receive_json_from()  # drain connect's own presence.joined broadcast

    await a.send_json_to({"action": "chat.stop", "data": {"message_id": "m1"}})
    assert await a.receive_nothing(timeout=1)
    await a.disconnect()


async def test_snapshot_falls_back_to_the_binding_tail():
    """A local runner session has NO Message rows — the panel must still open populated.

    Regression (prod): ChatPage's transcript comes from THIS snapshot, not from
    getSession, so patching only the REST path left every runner-discovered
    session rendering "Start the conversation" despite the binding holding a tail.
    """
    from apps.canopy_sessions.models import RunnerBinding
    from apps.harness.models import Runner

    owner, _t, session = await database_sync_to_async(_seed)()

    @database_sync_to_async
    def _bind():
        ws = session.workspace
        r = Runner.objects.create(
            name="jj-mbp", workspace=ws, location=Runner.LOCAL, paired_by=owner,
            host="jj@mbp", status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        )
        RunnerBinding.objects.create(
            session=session, runner=r, session_key="ace-demo", thread_key="emdash:ace-demo",
            host=r.host, last_interacted_at=timezone.now(),
            tail=[{"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1"}],
        )

    await _bind()
    comm = await _connect(session, owner)
    connected, _ = await comm.connect()
    assert connected is True
    snap = await _recv_match(comm, lambda f: f.get("event") == "session.state")
    msgs = snap["data"]["messages"]
    assert [m["plaintext"] for m in msgs] == ["q1", "a1"]
    assert [m["turn_index"] for m in msgs] == [-2, -1]   # never collides with real rows
    assert [m["id"] for m in msgs] == ["tail:-2", "tail:-1"]
    await comm.disconnect()


# ── origin derived from the session's owning product ────────────────────────
# THE path a human actually uses: canopy-ui's chat kit sends `chat.send` and the
# consumer calls send_message with no origin. #496 threaded origin only through
# ace-web's SERVER-side run dispatcher, so typing into ace-web's own chat still
# produced canopy_web_chat and routed like canopy chat — observed live, a
# "testing" message on an ace-web session went to a laptop runner. Deriving in
# the REST view alone would have left this path exactly as broken.
async def test_ws_send_on_an_ace_web_session_is_ace_web_work():
    def _seed_ace_web():
        owner, _teammate, session = _seed()
        session.metadata = {**(session.metadata or {}), "source": "ace-web",
                            "origin_key": "ace-web:dimagi-team"}
        session.save(update_fields=["metadata"])
        return owner, session

    owner, session = await database_sync_to_async(_seed_ace_web)()
    comm = await _connect(session, owner)
    assert (await comm.connect())[0]
    await comm.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "testing"}})
    await _recv_match(comm, lambda f: f.get("event") == "draft.updated")
    await comm.send_json_to({"action": "chat.send", "data": {}})
    await _recv_match(comm, lambda f: f.get("event") == "chat.stream_complete")

    origin = await database_sync_to_async(
        lambda: Turn.objects.filter(chat_session_id=session.id).first().origin
    )()
    assert origin == Turn.ORIGIN_ACE_WEB
    await comm.disconnect()


async def test_ws_send_on_a_canopy_session_is_still_chat():
    owner, _teammate, session = await database_sync_to_async(_seed)()
    comm = await _connect(session, owner)
    assert (await comm.connect())[0]
    await comm.send_json_to({"action": "draft.update", "data": {"version": 0, "body": "hi"}})
    await _recv_match(comm, lambda f: f.get("event") == "draft.updated")
    await comm.send_json_to({"action": "chat.send", "data": {}})
    await _recv_match(comm, lambda f: f.get("event") == "chat.stream_complete")

    origin = await database_sync_to_async(
        lambda: Turn.objects.filter(chat_session_id=session.id).first().origin
    )()
    assert origin == Turn.ORIGIN_CANOPY_WEB_CHAT
    await comm.disconnect()
