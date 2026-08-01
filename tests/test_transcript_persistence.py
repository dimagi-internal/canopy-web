"""Persist a runner session's transcript (durable, restart-proof live view).

Spec: docs/superpowers/specs/2026-07-24-persist-runner-session-transcript-design.md.
For an origin=runner session the laptop transcript is the single source of truth:
every Message.turn_index is the transcript record ordinal, so the live stream
(forward) and backfill (older) are the SAME rows by identity — idempotent
get_or_create, no collision. Web sessions are unchanged.
"""
import pytest
from channels.db import database_sync_to_async
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions import services
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx(*, origin=Session.ORIGIN_RUNNER, watched=True):
    """`watched` = a viewer is attached (RunnerBinding.stream_desired).

    Defaults True because these tests were written when fan-out was unconditional,
    so that is the behaviour they were asserting. It governs LIVE PUSH only — the
    durable write happens either way, which
    `test_stream_post_persists_for_an_unwatched_session` pins directly.
    """
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="jj-mbp", workspace=ws, location=Runner.LOCAL, paired_by=user,
        host="jj@mbp", status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    s = Session.objects.create(workspace=ws, origin=origin, created_by=user, title="ace-demo")
    RunnerBinding.objects.create(
        session=s, runner=runner, session_key="ace-demo",
        thread_key="emdash:ace-demo", host=runner.host, stream_desired=watched,
    )
    c = Client(); c.force_login(user)
    return user, ws, runner, s, c


def _rows(session):
    return [(m.turn_index, m.role, m.plaintext) for m in session.messages.order_by("turn_index")]


# --- persist_transcript_rows: one idempotent write path -------------------

def test_persist_rows_keys_on_the_transcript_ordinal_idempotently():
    _u, _w, _r, s, _c = _ctx()
    rows = [
        {"index": 5, "role": "user", "text": "q1"},
        {"index": 7, "role": "assistant", "text": "a1"},
    ]
    assert services.persist_transcript_rows(s, rows) == 2
    # Re-shipping the same records (stream retry, backfill overlap) is a no-op.
    assert services.persist_transcript_rows(s, rows) == 0
    assert _rows(s) == [(5, "user", "q1"), (7, "assistant", "a1")]


def test_persist_rows_assigns_sequentially_when_no_ordinal():
    """index=-1 (an old runner) => server assigns the next free index."""
    _u, _w, _r, s, _c = _ctx()
    rows = [
        {"index": -1, "role": "user", "text": "q1"},
        {"index": -1, "role": "assistant", "text": "a1"},
    ]
    assert services.persist_transcript_rows(s, rows) == 2
    assert _rows(s) == [(0, "user", "q1"), (1, "assistant", "a1")]


def test_persist_rows_skips_unknown_roles():
    _u, _w, _r, s, _c = _ctx()
    rows = [
        {"index": 3, "role": "status", "text": "nope"},
        {"index": 4, "role": "assistant", "text": "a"},
    ]
    assert services.persist_transcript_rows(s, rows) == 1
    assert _rows(s) == [(4, "assistant", "a")]


# --- the live stream persists (runner sessions, ordinals only) ------------

def _post_stream(c, runner, session, events):
    return c.post(
        f"/api/harness/runners/{runner.id}/session-stream",
        data={"session_id": str(session.id), "events": events},
        content_type="application/json",
    )


def test_stream_post_persists_ordinal_rows_and_fans_out_both_roles(monkeypatch):
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    _u, _w, runner, s, c = _ctx()
    body = _post_stream(c, runner, s, [
        {"kind": "user", "seq": 5, "index": 5, "payload": {"text": "q1"}},
        {"kind": "assistant", "seq": 7, "index": 7, "payload": {"text": "a1"}},
    ]).json()
    assert body == {"count": 2}
    assert _rows(s) == [(5, "user", "q1"), (7, "assistant", "a1")]
    # BOTH roles fan out now. User events used to be withheld on the grounds
    # that "the frontend already echoed them optimistically" — true for text
    # typed in the web, FALSE for text typed straight into emdash, which no web
    # client ever saw. That assumption silently dropped your own words from the
    # phone until a reload (observed 2026-07-27).
    kinds = [m["event"]["kind"] for _g, m in published]
    assert kinds == ["user", "assistant"]


def test_stream_post_persists_for_an_unwatched_session(monkeypatch):
    """Durability does not depend on anyone looking.

    The runner now tails every session it backs, not just attached ones, because
    the transcript is the durable source and gating the write on `stream_desired`
    made history a side effect of having the chat open: labs held 16% of its rows,
    8 of 12 sessions at zero. Rows persist; only the live push is withheld, since
    there is no client joined to that session group to push to.
    """
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    _u, _w, runner, s, c = _ctx(watched=False)
    body = _post_stream(c, runner, s, [
        {"kind": "user", "seq": 5, "index": 5, "payload": {"text": "q1"}},
        {"kind": "assistant", "seq": 7, "index": 7, "payload": {"text": "a1"}},
    ]).json()
    assert _rows(s) == [(5, "user", "q1"), (7, "assistant", "a1")]
    assert published == []
    assert body == {"count": 0}


def test_stream_post_without_ordinal_does_not_persist(monkeypatch):
    """An OLD runner (no index) keeps the legacy live-view-only contract: persisting
    assistant-only rows would kill the tail fallback's human side (trap 2)."""
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: None)
    _u, _w, runner, s, c = _ctx()
    _post_stream(c, runner, s, [{"kind": "assistant", "seq": 0, "payload": {"text": "a"}}])
    assert s.messages.count() == 0


def test_stream_post_never_persists_for_a_web_session(monkeypatch):
    """A web session's index space belongs to send_message/projection — the
    transcript stream must not write into it."""
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: None)
    _u, _w, runner, s, c = _ctx(origin=Session.ORIGIN_WEB)
    _post_stream(c, runner, s, [
        {"kind": "assistant", "seq": 9, "index": 9, "payload": {"text": "a"}},
    ])
    assert s.messages.count() == 0


# --- catch-up marker: GET /streams carries last_index ----------------------

def test_streams_descriptor_carries_last_index():
    _u, _w, runner, s, c = _ctx()
    RunnerBinding.objects.filter(session=s).update(stream_desired=True)
    body = c.get(f"/api/harness/runners/{runner.id}/streams").json()
    assert body["streams"][0]["last_index"] is None      # no rows yet
    Message.objects.create(session=s, turn_index=7, role=Message.ASSISTANT, plaintext="a")
    body = c.get(f"/api/harness/runners/{runner.id}/streams").json()
    assert body["streams"][0]["last_index"] == 7


# --- backfill: same rows by identity, fills the older gap ------------------

def test_write_backfill_upserts_older_rows_around_streamed_ones():
    _u, _w, _r, s, _c = _ctx()
    services.persist_transcript_rows(s, [{"index": 7, "role": "assistant", "text": "a2"}])
    full = [
        {"index": 1, "role": "user", "text": "q1"},
        {"index": 3, "role": "assistant", "text": "a1"},
        {"index": 5, "role": "user", "text": "q2"},
        {"index": 7, "role": "assistant", "text": "a2"},
    ]
    assert services.write_backfill(s, full) == 3          # 7 already exists
    assert _rows(s) == [
        (1, "user", "q1"), (3, "assistant", "a1"), (5, "user", "q2"), (7, "assistant", "a2"),
    ]
    assert services.write_backfill(s, full) == 0          # idempotent re-ship


def test_write_backfill_without_ordinals_keeps_the_write_once_contract():
    """An OLD runner ships {role,text} only — sequential, and only into an empty
    session, exactly as before."""
    _u, _w, _r, s, _c = _ctx()
    msgs = [{"role": "user", "text": "q"}, {"role": "assistant", "text": "a"}]
    assert services.write_backfill(s, msgs) == 2
    assert services.write_backfill(s, msgs) == 0
    assert _rows(s) == [(0, "user", "q"), (1, "assistant", "a")]


def test_request_backfill_requested_when_rows_lack_the_start(monkeypatch):
    """Streamed rows alone (ordinals > 0) are not the full history — 'ready' only
    when the start of the transcript (turn_index 0) is present."""
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: None)
    _u, _w, _r, s, c = _ctx()
    Message.objects.create(session=s, turn_index=3, role=Message.ASSISTANT, plaintext="a")
    assert c.post(f"/api/canopy-sessions/{s.id}/backfill").json() == {"status": "requested"}
    Message.objects.create(session=s, turn_index=0, role=Message.USER, plaintext="q")
    assert c.post(f"/api/canopy-sessions/{s.id}/backfill").json() == {"status": "ready"}


# --- send_message: the transcript is the sole durable source ---------------

def test_runner_session_send_writes_no_durable_user_row():
    user, _w, _r, s, _c = _ctx()
    msg, turn = services.send_message(session=s, text="hello", user=user, client_id="c1")
    assert s.messages.count() == 0                        # durable copy = transcript
    assert turn is not None and turn.status == Turn.QUEUED
    # The send contract still returns a message the client can render.
    assert msg.role == Message.USER and msg.plaintext == "hello"
    assert msg.pk and str(msg.pk) != "None"
    assert msg.created_at is not None


def test_runner_session_send_is_idempotent_per_client_id():
    user, _w, _r, s, _c = _ctx()
    _m1, turn1 = services.send_message(session=s, text="hello", user=user, client_id="c1")
    _m2, turn2 = services.send_message(session=s, text="hello", user=user, client_id="c1")
    assert turn1.id == turn2.id
    assert Turn.objects.filter(chat_session=s).count() == 1


def test_runner_session_sends_without_client_id_stay_distinct():
    """The WS path sends no client_id. Without a durable row, _next_index no longer
    advances between sends — the fallback key must still differ per send."""
    user, _w, _r, s, _c = _ctx()
    _m1, turn1 = services.send_message(session=s, text="one", user=user)
    _m2, turn2 = services.send_message(session=s, text="two", user=user)
    assert turn1.id != turn2.id
    assert Turn.objects.filter(chat_session=s).count() == 2


def test_web_session_send_still_writes_the_user_row():
    user, ws, _r, _s, _c = _ctx()
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="w")
    msg, _turn = services.send_message(session=s, text="hello", user=user, client_id="c1")
    assert s.messages.count() == 1
    assert msg.pk == s.messages.get().pk


def test_rest_send_returns_a_transient_message_for_runner_sessions():
    _u, _w, _r, s, c = _ctx()
    body = c.post(
        f"/api/canopy-sessions/{s.id}/send",
        data={"text": "hello", "client_id": "n1"},
        content_type="application/json",
    ).json()
    assert body["message"]["plaintext"] == "hello"
    assert body["message"]["role"] == "user"
    assert body["turn_id"]
    assert s.messages.count() == 0


# --- the ledger projection stays out of the transcript's index space -------

def test_project_events_skips_runner_sessions():
    """A bridged chat reply reaches the DB via the transcript stream (ordinal-keyed);
    projecting it AGAIN from the TurnEvent ledger would double-persist it."""
    from apps.harness import services as harness

    user, _w, _r, s, _c = _ctx()
    turn, _ = harness.enqueue_turn(session=s, origin=Turn.ORIGIN_API,
                                   idempotency_key="t1", prompt="hi")
    harness.append_events(turn, [{"kind": "assistant", "payload": {"text": "reply"}}])
    assert services.project_events(turn, list(turn.events.all())) == 0
    assert s.messages.count() == 0


def test_project_events_still_materializes_web_sessions():
    from apps.harness import services as harness

    user, ws, _r, _s, _c = _ctx()
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="w")
    turn, _ = harness.enqueue_turn(session=s, origin=Turn.ORIGIN_API,
                                   idempotency_key="t2", prompt="hi")
    harness.append_events(turn, [{"kind": "assistant", "payload": {"text": "reply"}}])
    assert services.project_events(turn, list(turn.events.all())) == 1
    assert s.messages.count() == 1


# --- the DoD flow: attach → persist → detach → catch-up → parity -----------

async def _ws_messages(session, user):
    from channels.testing import WebsocketCommunicator

    from apps.canopy_sessions.consumers import SessionConsumer

    comm = WebsocketCommunicator(SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{session.id}/")
    comm.scope["url_route"] = {"kwargs": {"session_id": str(session.id)}}
    comm.scope["user"] = user
    connected, _ = await comm.connect()
    assert connected is True
    for _ in range(14):
        frame = await comm.receive_json_from(timeout=5)
        if frame.get("event") == "session.state":
            await comm.disconnect()
            return frame["data"]["messages"]
    await comm.disconnect()
    raise AssertionError("no session.state frame")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_catch_up_fills_the_gap_and_both_transports_agree(monkeypatch):
    """Attach → stream persists → detach → the agent keeps writing → re-attach →
    catch-up ships everything after last_index → REST and the WS snapshot show the
    same complete, ordered conversation with no dup of the user's own send."""
    def _flow():
        user, _w, runner, s, c = _ctx()
        # Viewer attaches; the runner streams the live exchange with ordinals.
        assert c.post(f"/api/canopy-sessions/{s.id}/attach").json()["streaming"] is True
        # The human sends from the phone: optimistic echo only, no durable row.
        services.send_message(session=s, text="q1", user=user, client_id="n1")
        _post_stream(c, runner, s, [
            {"kind": "user", "seq": 5, "index": 5, "payload": {"text": "q1"}},
            {"kind": "assistant", "seq": 7, "index": 7, "payload": {"text": "a1"}},
        ])
        # The runner's catch-up marker is now server-side.
        RunnerBinding.objects.filter(session=s).update(stream_desired=True)
        body = c.get(f"/api/harness/runners/{runner.id}/streams").json()
        assert body["streams"][0]["last_index"] == 7
        # Viewer detaches; the agent keeps working while nobody watches.
        c.post(f"/api/canopy-sessions/{s.id}/detach")
        # Re-attach: the runner ships everything after last_index.
        c.post(f"/api/canopy-sessions/{s.id}/attach")
        _post_stream(c, runner, s, [
            {"kind": "user", "seq": 8, "index": 8, "payload": {"text": "q2"}},
            {"kind": "assistant", "seq": 9, "index": 9, "payload": {"text": "a2"}},
        ])
        rest = c.get(f"/api/canopy-sessions/{s.id}").json()["messages"]
        return user, s, rest

    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: None)
    user, session, rest = await database_sync_to_async(_flow)()
    expected = [(5, "q1"), (7, "a1"), (8, "q2"), (9, "a2")]
    assert [(m["turn_index"], m["plaintext"]) for m in rest] == expected
    ws_rows = await _ws_messages(session, user)
    assert [(m["turn_index"], m["plaintext"]) for m in ws_rows] == expected


# --- Ordinal scheme: rebuild rather than interleave ------------------------


def test_rows_written_under_a_superseded_ordinal_scheme_are_rebuilt():
    """turn_index is both the sort order and the paging cursor, so rows from two
    ordinal schemes in one session render the conversation shuffled — and
    get_or_create can't tell that an old row and a new one are the same record.
    The first write under the current scheme drops the stale rows instead."""
    _u, _ws, _r, s, _c = _ctx()
    # Pre-existing rows keyed the old way (turn_index == raw record ordinal).
    Message.objects.create(session=s, turn_index=0, role=Message.USER,
                           plaintext="old q", content={"text": "old q"})
    Message.objects.create(session=s, turn_index=1, role=Message.ASSISTANT,
                           plaintext="old a", content={"text": "old a"})
    assert s.ordinal_scheme == 0

    services.persist_transcript_rows(s, [
        {"index": 0, "role": "user", "text": "new q", "content": {"text": "new q"}},
        {"index": 64, "role": "assistant", "text": "new a", "content": {"text": "new a"}},
    ])

    s.refresh_from_db()
    assert s.ordinal_scheme == services.ORDINAL_SCHEME
    assert _rows(s) == [(0, "user", "new q"), (64, "assistant", "new a")]


def test_the_rebuild_happens_once_not_on_every_write():
    """It is a scheme migration, not a truncate-on-write: the second batch must
    add to the first, or a session would only ever hold its newest post."""
    _u, _ws, _r, s, _c = _ctx()
    services.persist_transcript_rows(s, [
        {"index": 0, "role": "user", "text": "q", "content": {"text": "q"}},
    ])
    services.persist_transcript_rows(s, [
        {"index": 64, "role": "assistant", "text": "a", "content": {"text": "a"}},
    ])
    assert _rows(s) == [(0, "user", "q"), (64, "assistant", "a")]


def test_a_legacy_ordinal_less_payload_never_triggers_a_rebuild():
    """An old runner that sends no ordinals says nothing about the scheme; wiping
    a session's history on its say-so would be destructive for no reason."""
    _u, _ws, _r, s, _c = _ctx()
    Message.objects.create(session=s, turn_index=0, role=Message.USER,
                           plaintext="kept", content={"text": "kept"})
    services.persist_transcript_rows(s, [{"index": -1, "role": "assistant", "text": "next"}])
    s.refresh_from_db()
    assert s.ordinal_scheme == 0
    assert [t for _i, _r2, t in _rows(s)] == ["kept", "next"]


def test_tool_rows_persist_their_structured_content():
    _u, _ws, _r, s, _c = _ctx()
    services.persist_transcript_rows(s, [
        {"index": 64, "role": "tool_use", "text": "",
         "content": {"id": "t1", "name": "Read", "input": {"file_path": "/x"}}},
        {"index": 128, "role": "tool_result", "text": "contents",
         "content": {"tool_use_id": "t1", "is_error": False}},
    ])
    use = Message.objects.get(session=s, turn_index=64)
    assert use.role == Message.TOOL_USE and use.content["name"] == "Read"
    res = Message.objects.get(session=s, turn_index=128)
    assert res.role == Message.TOOL_RESULT and res.content["tool_use_id"] == "t1"


def test_a_nul_byte_cannot_take_down_the_whole_batch():
    """Postgres rejects NUL in text/jsonb. The batch is ONE transaction, so an
    unscrubbed byte from a binary tool result (a Read of a compressed file) 500s
    every other row with it — and since the runner retries forever, that
    session's history could never rebuild. Labs 2026-07-26: one row in 683.

    Scrubbed server-side because the runner's own copy only covers runners that
    have been updated, and this is the single durable write path."""
    _u, _ws, _r, s, _c = _ctx()
    written = services.persist_transcript_rows(s, [
        {"index": 0, "role": "user", "text": "before\x00after",
         "content": {"text": "before\x00after"}},
        {"index": 64, "role": "tool_result", "text": "bin\x00ary",
         "content": {"tool_use_id": "t1", "is_error": False, "blob": "x\x00y"}},
    ])
    assert written == 2
    rows = list(s.messages.order_by("turn_index"))
    assert rows[0].plaintext == "beforeafter"
    assert rows[1].plaintext == "binary"
    assert rows[1].content["blob"] == "xy"          # nested strings too
    assert rows[1].content["tool_use_id"] == "t1"   # non-strings untouched
    assert rows[1].content["is_error"] is False


def test_content_does_not_duplicate_plaintext():
    """The wire payload and the stored row share one shape, but `text` is the
    one key storage doesn't need — plaintext is its own column and nothing on
    the render path reads content["text"]. Keeping it cost 36% of all stored
    transcript bytes (20,585 rows measured, 2026-07-26)."""
    _u, _ws, _r, s, _c = _ctx()
    services.persist_transcript_rows(s, [
        {"index": 0, "role": "assistant", "text": "hello", "content": {"text": "hello"}},
        {"index": 64, "role": "tool_result", "text": "a.txt",
         "content": {"tool_use_id": "t1", "is_error": False, "text": "a.txt"}},
    ])
    prose, result = list(s.messages.order_by("turn_index"))
    assert prose.plaintext == "hello" and prose.content == {}
    # Every OTHER key survives — those are what the UI pairs and renders on.
    assert result.plaintext == "a.txt"
    assert result.content == {"tool_use_id": "t1", "is_error": False}


def test_a_text_that_differs_from_plaintext_is_kept():
    """Only the redundant copy is dropped. A `text` that disagrees is real data
    and losing it would be a silent edit, not a saving."""
    _u, _ws, _r, s, _c = _ctx()
    services.persist_transcript_rows(s, [
        {"index": 0, "role": "assistant", "text": "shown",
         "content": {"text": "different", "id": "x"}},
    ])
    msg = s.messages.get()
    assert msg.content == {"text": "different", "id": "x"}


# --- the write is bulk, not row-at-a-time --------------------------------

def test_persist_rows_costs_a_bounded_number_of_queries():
    """The write must not be O(rows).

    It was: one `get_or_create` per row inside a single transaction. Measured on
    labs (2026-07-31), a 846-row backfill took ~14.6s end to end while the
    runner's own share — reading and parsing a 6.5 MB transcript — was 29 ms.
    The rest was 846 sequential round trips to RDS, and every path funnels
    through here (live stream, backfill, reset), so the cost is paid constantly.

    Bounded, not exact: the ordinal-scheme check, the existing-index probe, the
    insert batches and the session lock are all legitimate. O(n) is not.
    """
    _u, _w, _r, s, _c = _ctx()
    rows = [{"index": i, "role": "assistant", "text": f"a{i}"} for i in range(200)]
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        assert services.persist_transcript_rows(s, rows) == 200
    assert len(ctx.captured_queries) < 20, (
        f"{len(ctx.captured_queries)} queries for 200 rows — the write is still row-at-a-time"
    )


def test_persist_rows_dedupes_repeats_inside_one_batch():
    """Two rows with the same ordinal in ONE payload collapse to one row.

    `get_or_create` deduped this implicitly (the second call found the first).
    A bulk insert does not, and the pair violates the unique constraint, so the
    dedupe has to become explicit or the whole batch fails.
    """
    _u, _w, _r, s, _c = _ctx()
    written = services.persist_transcript_rows(s, [
        {"index": 4, "role": "assistant", "text": "first"},
        {"index": 4, "role": "assistant", "text": "second"},
    ])
    assert written == 1
    assert _rows(s) == [(4, "assistant", "first")]
