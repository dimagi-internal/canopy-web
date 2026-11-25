"""A chat reply leaves as soon as the agent hands back, and the turn reports its timings.

The fast pump runs between loop ticks, so a reply does not wait for the next tick
(poll_seconds) or the tick's claim and status reports. These hold that it finishes a
reply without a tick, leaves the tick-counted backstops alone, and that the finish
carries the runner's marks.
"""
import types

import pytest
from canopy_runner import cancel, chat_bridge, chat_pump, latency_marks
from canopy_runner.main import wait_pumping


def _asst(text, stop="tool_use"):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}],
                                            "stop_reason": stop}}


class _Reader:
    def __init__(self, batches):
        self.batches = list(batches)
        self.reads = 0

    def read_new(self):
        self.reads += 1
        return self.batches.pop(0) if self.batches else []


class _Client:
    def __init__(self):
        self.events, self.finished = [], []

    def post_events(self, turn_id, evs):
        self.events.extend(evs)

    def finish(self, turn_id, note="", status="done", emdash_task_id="", latency=None):
        self.finished.append({"turn": turn_id, "status": status, "latency": latency})


@pytest.fixture(autouse=True)
def _clean():
    chat_bridge.IN_FLIGHT.clear()
    cancel.CANCELLED_TURNS.clear()
    latency_marks.take("t1")
    yield
    chat_bridge.IN_FLIGHT.clear()
    cancel.CANCELLED_TURNS.clear()
    latency_marks.take("t1")


def _cfg():
    return types.SimpleNamespace(cdp_port=9222, runner_id="r1")


def _bridge(batches):
    bridge = chat_bridge.LiveBridge(turn_id="t1", task="cx-1", reader=_Reader(batches))
    chat_bridge.IN_FLIGHT["t1"] = bridge
    return bridge


def test_fast_pump_finishes_a_reply_the_moment_it_hands_back():
    client = _Client()
    _bridge([[_asst("Hi, I can help.", stop="end_turn")]])
    chat_pump.pump_chat_bridges(_cfg(), client, fast=True)
    assert [e["payload"]["text"] for e in client.events] == ["Hi, I can help."]
    assert [f["status"] for f in client.finished] == ["done"]
    assert chat_bridge.IN_FLIGHT == {}


def test_fast_pump_with_nothing_new_is_not_a_tick():
    """The idle backstop counts loop ticks (~5 s each); a fast look that found nothing
    must not advance it, or a silent tool call would trip it 20x sooner."""
    bridge = _bridge([[], [], []])
    for _ in range(3):
        chat_pump.pump_chat_bridges(_cfg(), _Client(), fast=True)
    assert bridge.ticks == 0 and bridge.idle_ticks == 0
    chat_pump.pump_chat_bridges(_cfg(), _Client())          # a real tick still counts
    assert bridge.ticks == 1 and bridge.idle_ticks == 1


def test_fast_pump_leaves_a_cancel_to_the_tick():
    client = _Client()
    _bridge([[_asst("partial")]])
    cancel.CANCELLED_TURNS.add("t1")
    chat_pump.pump_chat_bridges(_cfg(), client, fast=True)
    assert client.finished == [] and "t1" in chat_bridge.IN_FLIGHT


def test_finish_carries_the_runner_marks():
    client = _Client()
    latency_marks.fact("t1", warm=True)
    latency_marks.mark("t1", "prompt_delivered")
    _bridge([[_asst("Answer.", stop="end_turn")]])
    chat_pump.pump_chat_bridges(_cfg(), client, fast=True)
    sent = client.finished[0]["latency"]
    assert sent["warm"] is True
    assert {"prompt_delivered", "first_text", "reply_ready"} <= set(sent)
    assert latency_marks.take("t1") is None, "the marks travel once, on the finish"


def test_first_mark_wins():
    latency_marks.mark("t1", "first_text")
    first = latency_marks.take("t1")["first_text"]
    latency_marks.mark("t1", "first_text")
    latency_marks.mark("t1", "first_text")
    again = latency_marks.take("t1")
    assert again == {"first_text": again["first_text"]} and first <= again["first_text"]


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_wait_pumping_pumps_between_slices_while_a_reply_is_in_flight():
    clock, waits, pumps = _Clock(), [], []

    def wait_slice(s):
        waits.append(s)
        clock.now += s
        return False

    wait_pumping(1.0, wait_slice, lambda: pumps.append(clock.now), in_flight=lambda: True,
                 slice_seconds=0.25, clock=clock)
    assert len(pumps) == 3                       # at 0.25, 0.5, 0.75
    assert sum(waits) == pytest.approx(1.0)      # the wait is still the whole wait


def test_wait_pumping_is_one_plain_wait_with_nothing_in_flight():
    waits, pumps = [], []
    wait_pumping(5.0, lambda s: waits.append(s) or False, lambda: pumps.append(1),
                 in_flight=lambda: False, clock=_Clock())
    assert waits == [5.0] and pumps == []


def test_wait_pumping_returns_on_a_wake():
    clock, pumps = _Clock(), []

    def wait_slice(s):
        clock.now += s
        return True                              # a doorbell: claim now

    wait_pumping(5.0, wait_slice, lambda: pumps.append(1), in_flight=lambda: True,
                 slice_seconds=0.25, clock=clock)
    assert pumps == [] and clock.now == 0.25


def test_wait_pumping_stops_slicing_once_the_reply_has_gone():
    clock, flights, waits = _Clock(), [True, False], []

    def wait_slice(s):
        waits.append(s)
        clock.now += s
        return False

    wait_pumping(2.0, wait_slice, lambda: flights.pop(0) if len(flights) > 1 else None,
                 in_flight=lambda: flights[0], slice_seconds=0.25, clock=clock)
    assert waits == [0.25, pytest.approx(1.75)]
