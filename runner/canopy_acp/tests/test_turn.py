"""The shared turn loop (canopy_acp.turn.drive_turn), against a scripted agent.

Both runners drive their turns through this, so each rule here is held once for
the cloud runner and the laptop's `claude-cli` runtime alike. The cloud runner's
suite still runs its own turns through it end to end (runner/ec2/tests).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from canopy_acp import TurnError, UpdateReducer, drive_turn


def chunk(text):
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


def tool(call_id, status, **kw):
    kind = "tool_call" if status == "pending" else "tool_call_update"
    return {"sessionUpdate": kind, "toolCallId": call_id, "status": status, **kw}


class Agent:
    """Plays `updates` from inside `prompt`; records the calls the driver makes."""

    def __init__(self, cwd, env, on_update, *, updates=(), replay=(), stop="end_turn",
                 load_fails=False, result_raises=None, mode_refused=False, log=None):
        self.on_update, self.updates, self.replay = on_update, list(updates), list(replay)
        self.stop, self.load_fails, self.result_raises = stop, load_fails, result_raises
        self.mode_refused = mode_refused
        self.log = log if log is not None else []
        self.session_id = ""

    def start(self):
        self.log.append("start")

    def new_session(self, timeout=None):
        self.session_id = "new-sid"
        self.log.append("new_session")
        return self.session_id

    def load_session(self, sid, timeout=None):
        self.log.append(f"load:{sid}")
        for u in self.replay:
            self.on_update(sid, u)
        if self.load_fails:
            raise RuntimeError("no such session")
        self.session_id = sid

    def set_mode(self, mode_id):
        self.log.append(f"mode:{mode_id}")
        agent = self

        class Pending:
            def result(self, timeout=None):
                if agent.mode_refused:
                    raise RuntimeError("Invalid mode")
                return {}

        return Pending()

    def prompt(self, _text):
        self.log.append("prompt")
        for u in self.updates:
            self.on_update(self.session_id, u)
        agent = self

        class Pending:
            def result(self, timeout=None):
                if agent.result_raises:
                    raise agent.result_raises
                return {"stopReason": agent.stop}

        return Pending()

    def cancel(self):
        self.log.append("cancel")

    def exit_report(self):
        return "adapter exit code 1"

    def close(self):
        self.log.append("close")


def run(**agent_kw):
    events, log = [], []
    hooks = {k: agent_kw.pop(k) for k in ("resume", "on_session", "cancelled", "on_end", "mode")
             if k in agent_kw}

    def make_agent(cwd, env, on_update):
        return Agent(cwd, env, on_update, log=log, **agent_kw)

    res = drive_turn("go", make_agent=make_agent, reducer=UpdateReducer(), emit=events.extend,
                     cwd="/tmp", release_wait=0, **hooks)
    return res, events, log


def test_a_reply_is_emitted_whole_around_a_tool_call():
    res, events, _ = run(updates=[
        chunk("Check"), chunk("ing."),
        tool("c1", "pending", title="Terminal", rawInput={}),
        tool("c1", "in_progress", rawInput={"command": "ls"}),
        tool("c1", "completed", rawOutput="a.txt"),
        chunk("Do"), chunk("ne.")])
    assert res.ok and res.session_id == "new-sid"
    assert [(e["kind"], e["payload"].get("text")) for e in events] == [
        ("assistant", "Checking."), ("tool_start", None), ("tool_end", None), ("assistant", "Done.")]
    # The start waited for the arguments: never an empty Bash row.
    assert events[1]["payload"]["input"] == {"command": "ls"}


def test_a_resume_does_not_re_emit_the_replayed_history():
    res, events, log = run(resume="old-sid", replay=[chunk("earlier reply")],
                           updates=[chunk("new reply")])
    assert res.session_id == "old-sid" and "new_session" not in log
    assert [e["payload"]["text"] for e in events] == ["new reply"]
    assert res.text == "new reply"


def test_a_resume_that_cannot_load_starts_fresh_and_says_so():
    named = []
    res, _, log = run(resume="gone-sid", load_fails=True, on_session=named.append)
    assert res.ok and res.session_id == "new-sid" and named == ["new-sid"]
    assert log.index("load:gone-sid") < log.index("new_session") < log.index("prompt")


def test_the_mode_is_set_on_the_session_before_the_prompt():
    _, _, log = run(mode="bypassPermissions")
    assert log.index("new_session") < log.index("mode:bypassPermissions") < log.index("prompt")
    _, _, log = run(mode="bypassPermissions", resume="old-sid")
    assert log.index("load:old-sid") < log.index("mode:bypassPermissions") < log.index("prompt")


def test_a_refused_mode_still_runs_the_turn():
    res, _, log = run(mode="bypassPermissions", mode_refused=True)
    assert res.ok and "prompt" in log


def test_no_mode_leaves_the_session_as_the_adapter_starts_it():
    _, _, log = run()
    assert not any(e.startswith("mode:") for e in log)


def test_the_session_is_named_before_the_prompt_and_unregistered_before_close():
    order = []
    run(on_session=lambda sid: order.append("session"), on_end=lambda a: order.append("end"))
    assert order == ["session", "end"]


def test_a_cancel_is_sent_once_and_reported():
    res, _, log = run(cancelled=lambda: True, stop="cancelled")
    assert res.cancelled and not res.ok
    assert log.count("cancel") == 1


def test_a_turn_failed_on_its_stop_reason_names_it_after_the_reply():
    # Investigation 12: a failed turn's note was only the agent's last words.
    res, _, _ = run(updates=[chunk("I'll run it in the background.")], stop="refusal")
    assert not res.ok
    assert res.text == ("I'll run it in the background.\n\n"
                        "turn ended: refusal (adapter version unknown)")


class Versioned(Agent):
    def start(self):
        super().start()
        return {"agentInfo": {"name": "claude-agent-acp", "version": "0.93.0"}}

    def stderr_tail(self):
        return "Session new-sid, stopReason=cancelled"


def test_a_cancel_canopy_never_sent_is_named_the_adapters_with_its_version():
    # Investigation 14 (turn 068076b8, 2026-10-26): "turn ended: cancelled" with no
    # stop requested could not say who cancelled, or which adapter build the box ran.
    logged = []
    res = drive_turn("go", make_agent=lambda cwd, env, on_update: Versioned(
                         cwd, env, on_update, updates=[chunk("Running it in the background.")],
                         stop="cancelled"),
                     reducer=UpdateReducer(), emit=lambda e: None, cwd="/tmp",
                     release_wait=0, log=logged.append)
    assert not res.ok and res.cancelled
    assert res.text.endswith("turn ended: cancelled (the adapter's own: canopy sent no cancel; "
                             "claude-agent-acp 0.93.0)")
    # The adapter's stderr goes to the runner's log, never the note.
    assert "stopReason=cancelled" not in res.text
    assert any("stopReason=cancelled" in line for line in logged)


def test_a_cancel_canopy_sent_says_so_even_with_no_reply():
    res, _, _ = run(cancelled=lambda: True, stop="cancelled")
    assert res.text == "turn ended: cancelled (canopy sent the cancel; adapter version unknown)"


def test_a_clean_turn_text_is_the_reply_alone():
    res, _, _ = run(updates=[chunk("Done.")])
    assert res.ok and res.text == "Done."


def test_a_rejected_rate_limit_fails_a_clean_end_turn():
    reducer = UpdateReducer()
    reducer.rate_limit = {"status": "rejected", "rateLimitType": "five_hour"}
    res = drive_turn("go", make_agent=lambda cwd, env, on_update: Agent(cwd, env, on_update),
                     reducer=reducer, emit=lambda e: None, cwd="/tmp", release_wait=0)
    assert not res.ok and res.rate_limit["rateLimitType"] == "five_hour"


def test_a_dropped_connection_names_how_the_adapter_went():
    with pytest.raises(TurnError) as err:
        run(result_raises=RuntimeError("ACP connection closed"))
    assert err.value.detail == "ACP connection closed (adapter exit code 1)"
    assert err.value.session_id == "new-sid"


def test_a_silent_turn_is_cut_off_by_idle_time():
    now = {"t": 0.0}

    class Slow(Agent):
        def prompt(self, _text):
            class Pending:
                def result(self, timeout=None):
                    now["t"] += timeout
                    raise TimeoutError
            return Pending()

    log = []
    with pytest.raises(TurnError, match="silent"):
        drive_turn("go", make_agent=lambda cwd, env, on_update: Slow(cwd, env, on_update, log=log),
                   reducer=UpdateReducer(), emit=lambda e: None, cwd="/tmp", release_wait=0,
                   clock=lambda: now["t"], flush_seconds=10, idle_timeout=60)
    assert "cancel" in log and log[-1] == "close"


def test_the_turn_waits_for_the_sessions_last_process():
    left = {"n": 2}

    def processes(sid):
        left["n"] -= 1
        return [123] if left["n"] >= 0 else []

    drive_turn("go", make_agent=lambda cwd, env, on_update: Agent(cwd, env, on_update),
               reducer=UpdateReducer(), emit=lambda e: None, cwd="/tmp",
               release_wait=5, processes=processes)
    assert left["n"] < 0  # asked until none named the session


def test_on_windows_the_session_is_found_by_command_line_not_pgrep(monkeypatch):
    from canopy_acp import turn

    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return SimpleNamespace(stdout="4242\r\n")

    monkeypatch.setattr(turn.sys, "platform", "win32")
    monkeypatch.setattr(turn.subprocess, "run", run)
    assert turn.session_processes("sid-w") == [4242]
    assert seen[0][0] == "powershell" and "pgrep" not in seen[0]
    # The query's own command line names the session; it must not find itself.
    assert "$_.ProcessId -ne $PID" in seen[0][-1] and "*sid-w*" in seen[0][-1]
