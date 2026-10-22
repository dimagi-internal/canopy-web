"""TurnEvent -> canonical chat.* frame translation (pure)."""
from __future__ import annotations

from apps.canopy_sessions import stream_map
from apps.canopy_sessions.stream_map import turn_event_to_frames


def _rid(seq):
    return f"m:{seq}"


def test_assistant_maps_to_start_and_complete():
    frames = turn_event_to_frames(
        {"seq": 5, "kind": "assistant", "payload": {"text": "hello"}, "ts": "t"}, _rid)
    assert [f["event"] for f in frames] == ["chat.stream_start", "chat.stream_complete"]
    assert frames[0]["data"]["message_id"] == "m:5"
    assert frames[1]["data"]["plaintext"] == "hello"


def test_an_api_error_row_says_so_on_the_frame():
    """A watcher must see "didn't run" live, not the CLI's line as a reply."""
    frames = turn_event_to_frames(
        {"seq": 7, "kind": "assistant", "ts": "t",
         "payload": {"text": "Not logged in · Please run /login",
                     "api_error": "api_error", "signed_out": True}}, _rid)
    assert frames[1]["data"] == {"message_id": "m:7", "plaintext": "Not logged in · Please run /login",
                                 "api_error": "api_error", "signed_out": True}
    plain = turn_event_to_frames(
        {"seq": 8, "kind": "assistant", "payload": {"text": "hi"}, "ts": "t"}, _rid)
    assert "api_error" not in plain[1]["data"]


def test_tool_events_map():
    assert turn_event_to_frames(
        {"seq": 1, "kind": "tool_start", "payload": {"name": "Bash"}, "ts": "t"}, _rid
    )[0]["event"] == "chat.tool_use"
    assert turn_event_to_frames(
        {"seq": 2, "kind": "tool_end", "payload": {"ok": True}, "ts": "t"}, _rid
    )[0]["event"] == "chat.tool_result"


def test_tool_use_id_is_carried_through_to_the_client_frame():
    """RC/run-convergence PR3: with parallel tool calls, the client needs
    tool_use_id to pair a tool_end to its tool_start unambiguously. The
    ledger payload is forwarded to the client verbatim as `block`, so a
    producer that stamps the id gets it all the way to the frame with no
    extra plumbing here — this test pins that contract in place."""
    start_frames = turn_event_to_frames(
        {
            "seq": 1,
            "kind": "tool_start",
            "payload": {"id": "call-1", "name": "Bash", "input": {"command": "ls"}},
            "ts": "t",
        },
        _rid,
    )
    assert start_frames[0]["data"]["block"]["id"] == "call-1"
    assert start_frames[0]["data"]["block"]["input"] == {"command": "ls"}

    end_frames = turn_event_to_frames(
        {
            "seq": 2,
            "kind": "tool_end",
            "payload": {"tool_use_id": "call-1", "is_error": False, "content": "out"},
            "ts": "t",
        },
        _rid,
    )
    assert end_frames[0]["data"]["block"]["tool_use_id"] == "call-1"
    assert end_frames[0]["data"]["block"]["is_error"] is False


def test_tool_events_without_an_id_still_render():
    """Backward compatibility: older ledger rows / not-yet-updated runners
    have no tool_use_id at all. The frame must still be produced (the client
    is responsible for the FIFO fallback pairing, not this pure mapper)."""
    frames = turn_event_to_frames(
        {"seq": 1, "kind": "tool_start", "payload": {"name": "Bash"}, "ts": "t"}, _rid
    )
    assert frames[0]["event"] == "chat.tool_use"
    assert "id" not in frames[0]["data"]["block"]


def test_status_and_heartbeat_are_silent():
    assert turn_event_to_frames({"seq": 1, "kind": "status", "payload": {"status": "running"}, "ts": "t"}, _rid) == []
    assert turn_event_to_frames({"seq": 1, "kind": "heartbeat", "payload": {}, "ts": "t"}, _rid) == []


def test_error_maps_to_stream_error():
    frames = turn_event_to_frames({"seq": 9, "kind": "error", "payload": {"detail": "boom"}, "ts": "t"}, _rid)
    assert frames[0]["event"] == "chat.stream_error"
    assert frames[0]["data"]["detail"] == "boom"


def test_a_user_event_becomes_a_client_frame():
    """Text typed straight into emdash. It reaches no web client any other way —
    there was no optimistic echo, because no web client sent it. Before this,
    `stream_map` had no `user` branch at all and the message simply never
    appeared until a reload (observed 2026-07-27)."""
    frames = turn_event_to_frames(
        {"kind": "user", "seq": 64, "payload": {"text": "do the thing"}},
        lambda seq: f"seq:{seq}",
    )
    assert len(frames) == 1
    assert frames[0]["event"] == "chat.user_message"
    assert frames[0]["data"]["plaintext"] == "do the thing"
    # The ordinal rides along so the client upserts instead of appending.
    assert frames[0]["data"]["turn_index"] == 64


def test_a_user_event_carries_its_record_provenance():
    """A compaction summary is a `user` record nobody typed; its flags ride the
    live frame, as they ride a history row's `content["record"]`, so the client
    can tell it from a person's line without a reload."""
    frames = turn_event_to_frames(
        {"kind": "user", "seq": 7, "payload": {"text": "This session is being continued…",
                                               "record": {"compact_summary": True}}},
        _rid,
    )
    assert frames[0]["data"]["record"] == {"compact_summary": True}


def test_an_activity_event_becomes_a_status_frame():
    """Turn boundaries answer "is the agent working right now" — which nothing
    else could. Between a prompt and the first tool call, Claude is thinking and
    emits no content at all, so the session read as idle for the most
    interesting part of a turn."""
    for kind, state in (("activity:working", "working"), ("activity:idle", "idle")):
        frames = stream_map.turn_event_to_frames(
            {"kind": kind, "seq": -1, "payload": {}}, lambda seq: f"seq:{seq}")
        assert frames == [{"event": "session.activity", "data": {"state": state}}]


def test_a_blocked_activity_carries_its_menu():
    """"blocked" alone says somebody is wanted, not what is being asked — and
    without the question and its options there is nothing a phone can answer."""
    menu = {"question": "Do you want to proceed?", "title": "Bash command",
            "body": "rm target.txt",
            "options": [{"number": 1, "label": "Yes"}, {"number": 2, "label": "No"}]}
    frames = turn_event_to_frames(
        {"kind": "activity:blocked", "seq": -1, "payload": {"menu": menu}}, _rid)
    assert frames == [{"event": "session.activity",
                       "data": {"state": "blocked", "menu": menu}}]


def test_an_activity_without_a_menu_is_unchanged():
    """A runner with no CDP, or one whose read failed, still reports the state —
    the frame must not grow a null menu the client has to special-case."""
    frames = turn_event_to_frames({"kind": "activity:blocked", "seq": -1, "payload": {}}, _rid)
    assert frames == [{"event": "session.activity", "data": {"state": "blocked"}}]


def test_live_tool_rows_are_named_by_their_own_call_not_seq_minus_one():
    """Hook/ACP rows all carry seq -1, and `seq:-1` was every live row's id — so
    the client upserted each new call into the first row still holding it,
    rendering a running tool above the previous reply."""
    def live(kind, payload):
        return turn_event_to_frames({"seq": -1, "kind": kind, "payload": payload}, lambda s: f"seq:{s}")[0]["data"]

    a = live("tool_use", {"id": "toolu_A", "name": "Bash"})
    b = live("tool_use", {"id": "toolu_B", "name": "Read"})
    r = live("tool_result", {"tool_use_id": "toolu_A"})
    assert (a["tool_message_id"], b["tool_message_id"], r["tool_message_id"]) == (
        "live:use:toolu_A", "live:use:toolu_B", "live:result:toolu_A")
    # No ordinal rather than -1, which would sort above the whole conversation.
    assert a["turn_index"] is None

    durable = turn_event_to_frames({"seq": 64, "kind": "tool_use", "payload": {"id": "toolu_A"}}, _rid)[0]["data"]
    assert (durable["tool_message_id"], durable["turn_index"]) == ("m:64", 64)
