"""The tick pump that carries an in-flight chat reply back into the ledger.

This is the half that used to be an inline poll loop inside execute_chat_turn, where
it had to give up after 3s of transcript silence to avoid blocking the runner — and
giving up after 3s is what truncated every answer that involved a tool call.
"""
import types

import pytest
from canopy_runner import chat_bridge, main


def _asst(text, stop="tool_use"):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}],
                                            "stop_reason": stop}}


class _Reader:
    def __init__(self, batches):
        self.batches = list(batches)

    def read_new(self):
        return self.batches.pop(0) if self.batches else []


class _FakeClient:
    def __init__(self, post_fails=0):
        self.events = []
        self.finished = []
        self.heartbeats = []
        self._post_fails = post_fails

    def post_events(self, turn_id, evs):
        if self._post_fails > 0:
            self._post_fails -= 1
            raise RuntimeError("network")
        self.events.extend(evs)

    def finish(self, turn_id, note="", status="done"):
        self.finished.append((turn_id, status, note))

    def heartbeat(self, runner_id, active_turn_ids, **k):
        self.heartbeats.append(list(active_turn_ids))


@pytest.fixture(autouse=True)
def _clean_registry():
    chat_bridge.IN_FLIGHT.clear()
    main.CANCELLED_TURNS.clear()
    yield
    chat_bridge.IN_FLIGHT.clear()
    main.CANCELLED_TURNS.clear()


def _cfg():
    return types.SimpleNamespace(cdp_port=9222, runner_id="r1")


def _register(batches):
    bridge = chat_bridge.LiveBridge(turn_id="t1", task="echo-1", reader=_Reader(batches))
    chat_bridge.IN_FLIGHT["t1"] = bridge
    return bridge


def test_pump_streams_across_ticks_and_finishes_on_end_turn():
    """The regression: the preamble, a long silent tool call, then the real answer.
    All three ticks belong to ONE turn, and the turn ends only on the end marker."""
    client = _FakeClient()
    _register([
        [_asst("On it.")],                                  # tick 1: preamble
        [],                                                 # tick 2: a tool is running
        [_asst("The actual answer.", stop="end_turn")],     # tick 3: done
    ])
    main._pump_chat_bridges(_cfg(), client)
    assert [e["payload"]["text"] for e in client.events] == ["On it."]
    assert client.finished == [], "a silent tool call must not end the turn"

    main._pump_chat_bridges(_cfg(), client)
    assert client.finished == []

    main._pump_chat_bridges(_cfg(), client)
    assert [e["payload"]["text"] for e in client.events] == ["On it.", "The actual answer."]
    assert client.finished == [("t1", "done", "chat reply bridged (26 chars)")]
    assert chat_bridge.IN_FLIGHT == {}


def test_pump_retries_undelivered_text_instead_of_losing_it():
    client = _FakeClient(post_fails=1)
    _register([[_asst("the answer", stop="end_turn")], []])
    main._pump_chat_bridges(_cfg(), client)          # post blows up
    assert client.events == []
    assert client.finished == [], "don't finish a turn whose reply nobody received"
    main._pump_chat_bridges(_cfg(), client)          # next tick retries
    assert [e["payload"]["text"] for e in client.events] == ["the answer"]
    assert client.finished[0][1] == "done"


def test_pump_cancels_via_interrupt_and_finishes_cancelled(monkeypatch):
    from canopy_runner import cdp_control

    interrupted = {}
    monkeypatch.setattr(cdp_control, "interrupt",
                        lambda task, port=None: interrupted.update(task=task, port=port))

    client = _FakeClient()
    _register([[]])
    main.CANCELLED_TURNS.add("t1")
    main._pump_chat_bridges(_cfg(), client)

    assert interrupted == {"task": "echo-1", "port": 9222}
    assert client.finished == [("t1", "cancelled", "cancelled by user")]
    assert chat_bridge.IN_FLIGHT == {}
    assert "t1" not in main.CANCELLED_TURNS


def test_pump_cancel_survives_a_failed_interrupt(monkeypatch):
    """Cancel must not get stuck because the Escape press failed (emdash closed)."""
    from canopy_runner import cdp_control

    def _boom(task, port=None):
        raise RuntimeError("emdash gone")

    monkeypatch.setattr(cdp_control, "interrupt", _boom)
    client = _FakeClient()
    _register([[]])
    main.CANCELLED_TURNS.add("t1")
    main._pump_chat_bridges(_cfg(), client)
    assert client.finished == [("t1", "cancelled", "cancelled by user")]


def test_pump_survives_an_unreadable_transcript():
    class _Broken:
        def read_new(self):
            raise OSError("gone")

    client = _FakeClient()
    chat_bridge.IN_FLIGHT["t1"] = chat_bridge.LiveBridge(
        turn_id="t1", task="echo-1", reader=_Broken())
    main._pump_chat_bridges(_cfg(), client)          # must not raise
    assert client.finished == []
    assert "t1" in chat_bridge.IN_FLIGHT             # a quiet tick, not an ending


def test_pump_finish_failure_still_drops_the_bridge():
    """A failing finish must not leave the bridge pumping the same turn forever."""
    class _Client(_FakeClient):
        def finish(self, *a, **k):
            raise RuntimeError("server down")

    _register([[_asst("done", stop="end_turn")]])
    main._pump_chat_bridges(_cfg(), _Client())
    assert chat_bridge.IN_FLIGHT == {}


# --- Retained raw transcript (per turn) -------------------------------------


class _TranscriptClient:
    """Records transcript posts; can fail or report the per-turn ceiling."""

    def __init__(self, *, fail=False, truncate_after=None):
        self.posts = []           # (turn_id, lines, batch_id)
        self.fail = fail
        self.truncate_after = truncate_after
        self.events = []
        self.finished = []

    def post_transcript(self, turn_id, lines, batch_id=""):
        if self.fail:
            raise RuntimeError("network")
        self.posts.append((turn_id, lines, batch_id))
        if self.truncate_after is not None and len(self.posts) >= self.truncate_after:
            return False
        return True

    def post_events(self, turn_id, events):
        self.events.append((turn_id, events))

    def finish_turn(self, *a, **k):
        self.finished.append((a, k))

    def heartbeat(self, *a, **k):
        pass


class _RawReader:
    """A TailReader stand-in that also exposes last_raw."""

    def __init__(self, batches):
        self.batches = [(recs, raw) for recs, raw in batches]
        self.last_raw = []

    def read_new(self):
        if not self.batches:
            self.last_raw = []
            return []
        recs, raw = self.batches.pop(0)
        self.last_raw = list(raw)
        return recs


def test_the_turns_raw_jsonl_is_retained_verbatim(monkeypatch):
    """The retained transcript is the durable artifact cost and structure are
    re-derived from; it only works if the bytes are the CLI's own."""
    from canopy_runner import chat_bridge

    chat_bridge.IN_FLIGHT.clear()
    raw1 = ['{"type":"assistant","message":{"usage":{"input_tokens":5}}}']
    raw2 = ['{"type":"result","total_cost_usd":0.01}']
    bridge = chat_bridge.LiveBridge(
        turn_id="t1", task="task-1",
        reader=_RawReader([([_asst("working")], raw1),
                           ([_asst("done", stop="end_turn")], raw2)]),
    )
    chat_bridge.IN_FLIGHT["t1"] = bridge
    c = _TranscriptClient()
    cfg = _cfg()
    main._pump_chat_bridges(cfg, c)
    main._pump_chat_bridges(cfg, c)

    shipped = [line for _t, lines, _b in c.posts for line in lines]
    assert shipped == raw1 + raw2
    # batch_ids are sequential per turn, so a lost-ack retry dedupes server-side.
    assert [b for _t, _l, b in c.posts] == ["t1:0", "t1:1"]


def test_a_failed_flush_keeps_the_lines_for_the_next_tick():
    from canopy_runner import chat_bridge

    chat_bridge.IN_FLIGHT.clear()
    bridge = chat_bridge.LiveBridge(turn_id="t1", task="x", reader=_RawReader([]))
    bridge.raw_pending = ['{"a":1}']
    main._flush_turn_transcript(_TranscriptClient(fail=True), bridge)
    assert bridge.raw_pending == ['{"a":1}']    # nothing lost


def test_a_transcript_hiccup_never_holds_the_turn_open():
    """The reply is the turn's product; the transcript is derived. A storage
    problem must not make a finished agent look like it is still working."""
    from canopy_runner import chat_bridge

    bridge = chat_bridge.LiveBridge(turn_id="t1", task="x", reader=_RawReader([]))
    bridge.step([_asst("the answer", stop="end_turn")], ['{"raw":1}'])
    bridge.pending.clear()                      # the reply was delivered
    assert bridge.raw_pending                   # transcript still queued
    assert bridge.finished is True


def test_flushing_stops_once_the_server_reports_the_ceiling():
    from canopy_runner import chat_bridge

    bridge = chat_bridge.LiveBridge(turn_id="t1", task="x", reader=_RawReader([]))
    bridge.raw_pending = ['{"a":1}']
    c = _TranscriptClient(truncate_after=1)
    main._flush_turn_transcript(c, bridge)
    assert bridge.transcript_truncated is True
    bridge.step([], ['{"b":2}'])                # further lines aren't even queued
    assert bridge.raw_pending == []
    main._flush_turn_transcript(c, bridge)
    assert len(c.posts) == 1                    # and nothing more is sent


def test_batches_are_bounded_by_bytes_not_line_count():
    """The server 422s on total request bytes, and ONE tool-result line can be
    enormous — a count-based batch would sail past the cap."""
    from canopy_runner import chat_bridge

    small = ['{"n":%d}' % i for i in range(5)]
    batches = chat_bridge.chunk_raw_lines(small, max_bytes=25)
    assert len(batches) > 1
    for b in batches:
        assert sum(len(x.encode()) + 1 for x in b) <= 25
    assert [x for b in batches for x in b] == small


def test_an_oversized_single_line_becomes_a_visible_gap():
    """It can never fit, so shipping it would fail every retry. A marker means a
    cost aggregator SEES the gap instead of reading a partial turn as complete."""
    from canopy_runner import chat_bridge

    huge = '{"x":"' + "y" * 5000 + '"}'
    batches = chat_bridge.chunk_raw_lines(["{}", huge, "{}"], max_bytes=1000)
    flat = [x for b in batches for x in b]
    assert huge not in flat
    assert any("canopy_runner_line_dropped" in x for x in flat)
