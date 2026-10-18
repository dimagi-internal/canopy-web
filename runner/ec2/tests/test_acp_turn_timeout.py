"""The ACP wedge guard fails a SILENT turn, not a long one.

A 5400s wall-clock deadline killed ACE's full `/ace:run bednet-check-2-visit`
on cloud-ec2-2 while it was still logging tool calls every few seconds in
Phase 2 (turn 09d9322a, 2026-10-18, failure investigation 8). The guard is now
idle time since the agent's last update, with a far wall-clock backstop.
"""
from __future__ import annotations

from types import SimpleNamespace


class _Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now


class _Reducer:
    assistant_text = "done"
    rate_limit = None

    def apply(self, update):
        return update.get("sessionUpdate")

    def tool_call(self, call_id):
        return SimpleNamespace(raw_input={"command": "ls"}, is_complete=True, is_error=False,
                               tool_name="Bash", kind="execute", result_text="ok")

    def reset_stream_state(self):
        pass


def _run(cloud_runner, monkeypatch, tmp_path, *, polls, busy):
    """Run one ACP turn whose prompt stays pending for `polls` flush intervals,
    each one advancing the clock by a flush interval; `busy` makes the agent send
    a tool call on every poll."""
    clock = _Clock()
    monkeypatch.setattr(cloud_runner, "time", SimpleNamespace(time=clock.time, sleep=lambda s: None))
    cancelled = []

    class Agent:
        session_id = "acp-t"

        def __init__(self, cwd, env, on_update):
            self.on_update = on_update

        def start(self):
            pass

        def new_session(self, timeout=None):
            return self.session_id

        def prompt(self, _p):
            agent = self
            left = {"n": polls}

            class Pending:
                def result(self, timeout=None):
                    if left["n"] <= 0:
                        return {"stopReason": "end_turn"}
                    left["n"] -= 1
                    clock.now += cloud_runner.TRANSCRIPT_FLUSH_SECONDS
                    if busy:
                        agent.on_update(agent.session_id, {
                            "sessionUpdate": "tool_call", "toolCallId": f"c{left['n']}"})
                    raise TimeoutError

            return Pending()

        def cancel(self):
            cancelled.append(True)

        def close(self):
            pass

    class Core:
        AcpAgent = Agent
        UpdateReducer = _Reducer

    monkeypatch.setattr(cloud_runner, "_acp_core", lambda: Core)
    monkeypatch.setattr(cloud_runner, "_agent_env", lambda slug: {})
    ok, note, _ = cloud_runner.run_acp("/ace:run x", "t-long", lambda e: None, cwd=tmp_path)
    return ok, note, cancelled


def test_a_busy_turn_outlives_the_old_90_minute_deadline(cloud_runner, monkeypatch, tmp_path):
    two_hours = int(7200 / cloud_runner.TRANSCRIPT_FLUSH_SECONDS)
    ok, note, cancelled = _run(cloud_runner, monkeypatch, tmp_path, polls=two_hours, busy=True)
    assert ok, note
    assert not cancelled


def test_a_silent_turn_is_still_cut_off(cloud_runner, monkeypatch, tmp_path):
    polls = int(cloud_runner.ACP_TURN_IDLE_TIMEOUT_SECONDS / cloud_runner.TRANSCRIPT_FLUSH_SECONDS) + 3
    ok, note, cancelled = _run(cloud_runner, monkeypatch, tmp_path, polls=polls, busy=False)
    assert not ok
    assert "silent" in note
    assert cancelled


def test_the_backstop_still_ends_a_turn_that_never_stops(cloud_runner, monkeypatch, tmp_path):
    polls = int(cloud_runner.ACP_TURN_TIMEOUT_SECONDS / cloud_runner.TRANSCRIPT_FLUSH_SECONDS) + 3
    ok, note, cancelled = _run(cloud_runner, monkeypatch, tmp_path, polls=polls, busy=True)
    assert not ok
    assert "exceeded" in note
    assert cancelled
