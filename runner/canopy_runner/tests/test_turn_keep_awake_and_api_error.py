"""A laptop turn must not sleep mid-response, and one that died on an API error is a failure.

An emdash turn reads DONE at spawn, so a Mac that idle-slept mid-generation ("API Error:
Your computer went to sleep mid-response") left no trace. The hook listener holds the Mac
awake while a session is in-turn, and reports a turn that ends on a generic API error.
"""
import json
from types import SimpleNamespace

from canopy_runner import caller, hooks
from canopy_runner.hook_listener import HookListener, keep_awake_for

CWD = "/Users/x/emdash/worktrees/canopy-web/emdash/canopy-web-chat-1706-g225m"
KEY = ("canopy-web", "canopy-web-chat-1706")
PROMPT = {"hook_event_name": "UserPromptSubmit", "cwd": CWD}
SLEEP = "API Error: Your computer went to sleep mid-response. The response above may be incomplete."


class Holder:
    def __init__(self):
        self.released = False

    def terminate(self):
        self.released = True


def _transcript(tmp_path, text=SLEEP, *, uuid="u-1", error=None, api_error=True):
    t = tmp_path / "t.jsonl"
    rec = {"type": "assistant", "uuid": uuid, "isApiErrorMessage": api_error,
           "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]}}
    if error:
        rec["error"] = error
    t.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "go"}}) + "\n"
                 + json.dumps(rec) + "\n"
                 + json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n")
    return t


def _ping(t):
    return {"hook_event_name": "Notification", "cwd": CWD, "transcript_path": str(t),
            "message": "Claude is waiting for your input"}


def _listener(**kw):
    return HookListener(port=0, nonce="n", resolve_session=lambda cwd: "",
                        forward=lambda: False,
                        resolve_task=lambda cwd: [KEY] if cwd == CWD else [], **kw)


# ---- A: keep awake ----------------------------------------------------------------


def test_the_mac_is_held_awake_exactly_while_a_session_is_in_turn():
    held = []
    holder = Holder()
    hl = _listener(keep_awake=lambda pid: held.append(pid) or holder)
    hl.handle_payload({"hook_event_name": "SessionStart", "cwd": CWD})
    assert held == []
    hl.handle_payload(PROMPT)
    assert len(held) == 1 and not holder.released
    hl.handle_payload({"hook_event_name": "PostToolUse", "cwd": CWD, "tool_name": "Bash"})
    assert len(held) == 1                       # one hold, not one per hook
    hl.handle_payload({"hook_event_name": "Stop", "cwd": CWD})
    assert holder.released


def test_a_turn_ended_by_an_api_error_releases_the_hold(tmp_path):
    holder = Holder()
    hl = _listener(keep_awake=lambda pid: holder)
    hl.handle_payload(PROMPT)
    hl.handle_payload(_ping(_transcript(tmp_path)))
    assert holder.released


def test_a_session_that_never_stopped_cannot_hold_the_mac_forever(monkeypatch):
    from canopy_runner import hook_listener

    holder = Holder()
    hl = _listener(keep_awake=lambda pid: holder)
    hl.handle_payload(PROMPT)
    monkeypatch.setattr(hook_listener, "AWAKE_SILENCE_LIMIT", -1)
    hl.handle_payload({"hook_event_name": "SessionStart", "cwd": "/elsewhere"})
    assert holder.released


def test_caffeinate_is_idle_sleep_only_and_macos_only(monkeypatch):
    from canopy_runner import hook_listener

    calls = []
    monkeypatch.setattr(hook_listener.shutil, "which", lambda name: "/usr/bin/caffeinate")
    monkeypatch.setattr(hook_listener.sys, "platform", "linux")
    assert keep_awake_for(7, spawn=lambda *a, **k: calls.append(a)) is None and calls == []
    monkeypatch.setattr(hook_listener.sys, "platform", "darwin")
    keep_awake_for(7, spawn=lambda argv, **k: calls.append(argv) or "proc")
    assert calls == [["caffeinate", "-i", "-w", "7"]]


# ---- B: report an API-error ending ----------------------------------------------------


def test_an_api_error_tail_on_a_canopy_turn_is_reported(tmp_path):
    seen = []
    hl = _listener(on_api_error=lambda payload, keys, text: seen.append((keys, text)),
                   keep_awake=lambda pid: Holder())
    hl.handle_payload(PROMPT)
    hl.handle_payload(_ping(_transcript(tmp_path)))
    hl.handle_payload(PROMPT)
    hl.handle_payload(_ping(_transcript(tmp_path)))   # the same record again: once
    assert seen == [([KEY], SLEEP)]


def test_a_usage_cap_or_signed_out_ending_is_not_an_api_error_failure(tmp_path):
    seen = []
    hl = _listener(on_api_error=lambda *a: seen.append(a), keep_awake=lambda pid: Holder())
    hl.handle_payload(PROMPT)
    hl.handle_payload(_ping(_transcript(tmp_path, "You've hit your session limit · resets 2:30am",
                                        error="rate_limit")))
    hl.handle_payload(PROMPT)
    hl.handle_payload(_ping(_transcript(tmp_path, "Not logged in · Please run /login", uuid="u-2")))
    assert seen == []


def test_an_agent_merely_writing_api_error_is_not_a_failure(tmp_path):
    seen = []
    hl = _listener(on_api_error=lambda *a: seen.append(a), keep_awake=lambda pid: Holder())
    hl.handle_payload(PROMPT)
    hl.handle_payload(_ping(_transcript(tmp_path, api_error=False)))
    assert seen == []


def test_a_session_that_was_not_mid_turn_is_not_reported(tmp_path):
    seen = []
    hl = _listener(on_api_error=lambda *a: seen.append(a), keep_awake=lambda pid: Holder())
    hl.handle_payload(_ping(_transcript(tmp_path)))   # no UserPromptSubmit seen
    assert seen == []


def test_the_report_aborts_the_turn_canopy_launched_in_that_task(tmp_path, monkeypatch):
    monkeypatch.setattr(caller, "CALLER_ROOT", tmp_path)
    aborted = []
    client = SimpleNamespace(abort_turn=lambda tid, reason, tool="": aborted.append((tid, reason)))
    tid = "f357b44c-e3fd-404b-9208-c8e2aa98f5ed"
    caller.note_task_turn("canopy-web-chat-1706", tid)

    class Inline:                       # run the post on this thread
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(hooks.threading, "Thread", Inline)
    hooks.report_api_error(client, {}, [KEY], SLEEP)
    hooks.report_api_error(client, {}, [("canopy-web", "someone-elses-task")], SLEEP)
    assert [a[0] for a in aborted] == [tid]
    assert "went to sleep" in aborted[0][1]
