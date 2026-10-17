"""A person's stop reaches a turn running on this box.

2026-10-17, cloud-ec2-2: canopy-web sent `session_interrupt` (and would have sent
`cancel`); the WS loop had no branch for either, nothing read a heartbeat reply
for stops, and `stop_turn` was never called. The huddle turn ran 23 more minutes.

Now all three signals — the `cancel` frame, the `session_interrupt` frame and
`cancel_turn_ids` on any heartbeat reply — reach `request_stop`, which interrupts
the executor (ACP `session/cancel`, SIGTERM to `claude -p`), escalates if it is
ignored, and the turn's worker finishes it CANCELLED.
"""
from __future__ import annotations

import re
import threading

import pytest

TID = "fa383225-d697-4d9f-91f0-336b0ba10752"


class _Agent:
    def __init__(self):
        self.cancels = 0
        self.closed = threading.Event()

    def cancel(self):
        self.cancels += 1

    def close(self):
        self.closed.set()


class _Proc:
    def __init__(self):
        self.signals = []
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.signals.append("TERM")

    def kill(self):
        self.signals.append("KILL")


@pytest.fixture
def running(cloud_runner):
    with cloud_runner._TURNS_LOCK:
        cloud_runner._IN_FLIGHT[TID] = None
    yield cloud_runner
    with cloud_runner._TURNS_LOCK:
        cloud_runner._IN_FLIGHT.pop(TID, None)
    cloud_runner._acp_unregister(TID)
    cloud_runner._clear_stop(TID)


def test_a_turn_not_running_here_is_not_stopped(cloud_runner):
    assert cloud_runner.request_stop("nope") is False
    assert not cloud_runner.stop_requested("nope")


def test_stop_cancels_the_acp_session_then_closes_it_if_ignored(running, monkeypatch):
    mod = running
    agent = _Agent()
    mod._acp_register(TID, agent)
    clock = [1000.0]
    monkeypatch.setattr(mod.time, "time", lambda: clock[0])

    assert mod.request_stop(TID) is True
    assert agent.cancels == 1 and mod.stop_requested(TID)

    clock[0] += mod.STOP_ESCALATE_SECONDS + 1  # the next heartbeat re-delivers it
    mod.request_stop(TID)
    assert agent.closed.wait(2), "an ignored stop never escalated to closing the adapter"


def test_stop_terminates_claude_p_then_kills_it_if_ignored(running, monkeypatch):
    mod = running
    proc = _Proc()
    mod._register_cli_proc(TID, proc)
    clock = [1000.0]
    monkeypatch.setattr(mod.time, "time", lambda: clock[0])

    mod.request_stop(TID)
    assert proc.signals == ["TERM"]
    clock[0] += mod.STOP_ESCALATE_SECONDS + 1
    mod.request_stop(TID)
    assert proc.signals == ["TERM", "KILL"]


def test_a_stop_that_beat_the_spawn_stops_the_process_at_once(running):
    mod = running
    mod.request_stop(TID)
    proc = _Proc()
    mod._register_cli_proc(TID, proc)
    assert proc.signals == ["TERM"]


def test_a_stopped_turn_never_starts_an_executor(running, monkeypatch):
    mod = running
    monkeypatch.setattr(mod, "run_claude", lambda *a, **k: pytest.fail("ran a stopped turn"))
    monkeypatch.setattr(mod, "run_acp", lambda *a, **k: pytest.fail("ran a stopped turn"))
    mod.request_stop(TID)
    ok, _text, _sid = mod._execute_once("p", TID, lambda e: None)
    assert ok is False


def test_a_heartbeat_reply_naming_the_turn_stops_it(running):
    mod = running
    agent = _Agent()
    mod._acp_register(TID, agent)
    mod.honour_stops({"cancel_turn_ids": [TID], "refresh_pending": False})
    assert agent.cancels == 1
    mod.honour_stops(None)  # a failed beat is not a stop
    mod.honour_stops({})


def test_a_session_interrupt_stops_the_turn_driving_that_session(running):
    mod = running
    agent = _Agent()
    mod._acp_register(TID, agent)
    mod._announce_session(TID, "f988d1eb-02be-44c9-b301-2f87232da936")
    assert mod.stop_session("someone-else") == []
    assert mod.stop_session("f988d1eb-02be-44c9-b301-2f87232da936") == [TID]
    assert agent.cancels == 1


def test_the_ws_loop_and_every_heartbeat_honour_stops(cloud_runner):
    """The branches that were missing. Source-level, because the WS loop is a live
    socket — and an unhandled frame is indistinguishable from one never sent."""
    src = open(cloud_runner.__file__).read()
    ws = src[src.index("def run_over_ws"):src.index("def main()")]
    assert re.search(r'mtype == "cancel":\s*\n(\s*#.*\n)*\s*request_stop\(', ws)
    assert re.search(r'mtype == "session_interrupt":\s*\n(\s*#.*\n)*\s*stop_session\(', ws)
    assert "honour_stops(ack)" in ws
    rest = src[src.index("def run_over_rest"):src.index("def _ws_url")]
    assert "honour_stops(beat)" in rest
    lease = src[src.index("def _start_lease_renewal"):src.index("def pair_or_load")]
    assert "honour_stops(reply)" in lease


# ── the worker finishes a stopped turn CANCELLED ────────────────────────────

@pytest.fixture
def run(running, monkeypatch, tmp_path):
    mod = running
    monkeypatch.setattr(mod, "_turn_cwd", lambda turn, tid, env=None: tmp_path)
    monkeypatch.setattr(mod, "_start_lease_renewal", lambda rid, tid: threading.Event())
    monkeypatch.setattr(mod, "_child_safe_env", lambda: {"PATH": "/bin"})
    monkeypatch.setattr(mod, "_ship_transcript_rows", lambda *a, **k: None)
    timeline = []
    monkeypatch.setattr(mod, "_api",
                        lambda m, path, body=None, **k: timeline.append((path, body)) or (200, {}))
    return mod, timeline


def _turn():
    return {"id": TID, "agent_slug": "ada", "prompt": "/canopy:huddle --group-run 1",
            "origin_ref": {}}


def test_a_stopped_turn_finishes_cancelled_not_failed(run, monkeypatch):
    mod, timeline = run

    def execute(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        mod._announce_session(turn_id, "sid-1")
        mod.request_stop(turn_id, "cancel frame")      # a person pressed stop mid-turn
        return False, "turn ended: cancelled", "sid-1"

    monkeypatch.setattr(mod, "execute_prompt", execute)
    mod._run_turn("r-1", _turn())
    [finish] = [b for p, b in timeline if p == f"/turns/{TID}/finish"]
    assert finish["status"] == "cancelled"
    assert finish["session_key"] == "sid-1"
    assert not mod.stop_requested(TID), "the stop outlived its turn"


def test_a_turn_that_completed_despite_the_stop_stays_done(run, monkeypatch):
    """Truthful outcome: the server notes the ignored stop itself."""
    mod, timeline = run

    def execute(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        mod.request_stop(turn_id)
        return True, "All done.", "sid-1"

    monkeypatch.setattr(mod, "execute_prompt", execute)
    mod._run_turn("r-1", _turn())
    [finish] = [b for p, b in timeline if p == f"/turns/{TID}/finish"]
    assert finish["status"] == "done"


def test_a_stopped_turn_never_fails_over_to_another_credential(running, monkeypatch):
    mod = running
    calls = []

    def once(*a, **k):
        calls.append(1)
        mod.request_stop(TID)
        return False, "You've hit your usage limit", ""

    monkeypatch.setattr(mod, "_execute_once", once)
    monkeypatch.setattr(mod, "_advance_claude_credential", lambda **k: pytest.fail("failed over"))
    mod.execute_prompt("p", TID, lambda e: None)
    assert calls == [1]
