"""The Claude desktop session runtime (canopy_runner/desktop.py, canopy-web#1188).

Which backend a turn gets, and one turn's whole loop with the mod's half of the
file protocol simulated — no Claude app. The live half (a real app importing the
session, the real mod submitting) was proven by hand on a background macOS user.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from canopy_runner import desktop


class FakeClient:
    def __init__(self, plan=None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.plan = plan or {"reuse": False}

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self.plan if name == "resolve_session" else {}
        return call

    def names(self):
        return [c[0] for c in self.calls]

    def of(self, name):
        return [c for c in self.calls if c[0] == name]


@pytest.fixture(autouse=True)
def fresh_runtime(monkeypatch):
    monkeypatch.setattr(desktop, "_current", desktop.EMDASH)
    monkeypatch.setattr(desktop, "IN_FLIGHT", {})
    yield


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(desktop_dir=str(tmp_path / "desktop"), emdash_db="",
                           desktop_projects={}, desktop_allow=None, desktop_model="")


def _turn(**kw):
    t = {"id": "turn-0001-aaaa", "project": "scratch", "prompt": "do it",
         "workspace_slug": "dimagi", "origin_ref": {"thread_key": "th"}}
    t.update(kw)
    return t


# ── which backend ───────────────────────────────────────────────────────────

def test_observe_adopts_only_known_runtimes(monkeypatch):
    monkeypatch.setattr(desktop, "ensure_mod", lambda: None)
    desktop.observe({"engine": "claude-desktop"})
    assert desktop.current() == "claude-desktop"
    desktop.observe({})  # an old server says nothing: keep what we have
    desktop.observe({"engine": "tmux"})
    assert desktop.current() == "claude-desktop"
    desktop.observe({"engine": "emdash"})
    assert desktop.current() == "emdash"


def test_an_emdash_box_with_no_desktop_sessions_costs_nothing(cfg):
    client = FakeClient()
    assert desktop.maybe_execute(cfg, client, "r", _turn(), "th") is None
    assert client.calls == []  # not even a resolve-session round trip


def test_an_emdash_thread_stays_in_emdash_after_the_flip(cfg, monkeypatch):
    monkeypatch.setattr(desktop, "_current", desktop.CLAUDE_DESKTOP)
    client = FakeClient(plan={"reuse": True, "emdash_task_id": "hal-turn-abc"})
    assert desktop.maybe_execute(cfg, client, "r", _turn(), "th") is None


def test_a_desktop_thread_stays_in_desktop_after_flipping_back(cfg, monkeypatch):
    desktop._remember(cfg, "sid-9", Path(cfg.desktop_dir) / "wt", "scratch")
    started = []
    monkeypatch.setattr(desktop.TurnRun, "run", lambda self: started.append(self.reuse))
    client = FakeClient(plan={"reuse": True, "emdash_task_id": "sid-9"})
    action = desktop.maybe_execute(cfg, client, "r", _turn(), "th")
    assert action.startswith("desktop:reuse:")
    for th in list(desktop.IN_FLIGHT.values()):
        th.join()
    assert started == ["sid-9"]


def test_a_callers_confined_turn_never_goes_to_desktop(cfg, monkeypatch):
    monkeypatch.setattr(desktop, "_current", desktop.CLAUDE_DESKTOP)
    monkeypatch.setattr(desktop.caller, "capability", lambda turn: {"name": "ask"})
    assert desktop.maybe_execute(cfg, FakeClient(), "r", _turn(), "th") is None


def test_a_new_thread_takes_the_current_runtime(cfg, monkeypatch):
    monkeypatch.setattr(desktop, "_current", desktop.CLAUDE_DESKTOP)
    monkeypatch.setattr(desktop.TurnRun, "run", lambda self: None)
    assert desktop.maybe_execute(cfg, FakeClient(), "r", _turn(), "th").startswith("desktop:create:")


# ── helpers ─────────────────────────────────────────────────────────────────

def test_worktree_settings_merge_allow_and_enable_the_mod():
    out = desktop.worktree_settings(["Bash"], {"permissions": {"allow": ["Read"]}, "x": 1})
    assert out["permissions"]["allow"] == ["Bash", "Read"]
    assert out["enabledPlugins"][desktop.PLUGIN] is True and out["x"] == 1


def test_events_from_transcript_records():
    a = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hi"}, {"type": "tool_use", "id": "u1", "name": "Bash", "input": {}}]}}
    u = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "u1", "content": [{"type": "text", "text": "ok"}]}]}}
    assert [e["kind"] for e in desktop.events_from_record(a)] == ["assistant", "tool_start"]
    assert desktop.events_from_record(u)[0]["payload"]["content"] == "ok"


def test_batches_respect_the_byte_cap():
    assert desktop.batches(["a" * 6, "b" * 6, "c" * 6], limit=13) == [["a" * 6, "b" * 6], ["c" * 6]]


def test_transcript_for_answers_only_for_desktop_sessions(tmp_path):
    idx = tmp_path / ".canopy" / "desktop" / "sessions.json"
    idx.parent.mkdir(parents=True)
    idx.write_text(json.dumps({"sid-1": {"worktree": "/x"}}))
    proj = tmp_path / "claude" / "-some-dir"
    proj.mkdir(parents=True)
    (proj / "sid-1.jsonl").write_text("{}\n")
    kw = {"home": tmp_path, "claude_home": tmp_path / "claude"}
    assert desktop.transcript_for("sid-1", **kw) == proj / "sid-1.jsonl"
    assert desktop.transcript_for("hal-emdash-task", **kw) is None


# ── one turn, end to end, with the mod simulated ────────────────────────────

def _repo(tmp_path) -> Path:
    repo = tmp_path / "scratch"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    for argv in (["git", "init", "-q", "-b", "main"], ["git", "commit", "-q", "--allow-empty", "-m", "i"]):
        subprocess.run(argv, cwd=repo, check=True, env=env)
    return repo


def _mod(channel: Path, which: str, sid: str, answer: str):
    target = channel / ("task.txt" if which == "task" else f"{which}.txt")
    for _ in range(300):
        if target.exists() and (which != "task" or (channel / "seeded").exists()):
            break
        time.sleep(0.02)
    t = int(time.time() * 1000)
    for i, (kind, extra) in enumerate([("submitted", {"which": which}), ("ask", {"tool": "Bash"}),
                                       ("turn.complete", {"answer": answer})]):
        (channel / f"ev-{t + i}-{i}-{kind}.json").write_text(
            json.dumps({"t": t + i, "sid": sid, "kind": kind, "extra": extra}))


@pytest.fixture
def no_app(monkeypatch):
    opened = []
    monkeypatch.setattr(desktop, "ensure_app", lambda: True)
    monkeypatch.setattr(desktop, "open_session", opened.append)
    monkeypatch.setattr(desktop, "prepare_settings", lambda cfg, wt: None)
    monkeypatch.setattr(desktop, "seed_session", lambda cfg, wt: "sid-1")
    monkeypatch.setattr(desktop.caller, "write_caller_file", lambda turn: None)
    return opened


def _channel_of(cfg, name_part: str) -> Path:
    for _ in range(300):
        hits = list((Path(cfg.desktop_dir) / "worktrees").glob(f"*/*/{desktop.CHANNEL}"))
        if hits:
            return hits[0]
        time.sleep(0.02)
    raise AssertionError("no worktree was made")


def test_a_chat_turn_runs_to_completion_in_a_new_session(cfg, tmp_path, no_app):
    cfg.desktop_projects = {"scratch": str(_repo(tmp_path))}
    turn = _turn(origin_ref={"thread_key": "th", "chat_session_id": "chat-1"})
    client = FakeClient()
    run = desktop.TurnRun(cfg, client, "r", turn, "th", {"reuse": False}, reuse="")
    th = threading.Thread(target=run.run)
    th.start()
    _mod(_channel_of(cfg, "scratch"), "task", "sid-1", "all done")
    th.join(10)

    assert no_app == ["sid-1"]  # imported into the app once
    assert client.of("start")[0][1] == ("turn-0001-aaaa", "sid-1")
    assert client.of("record_session")[0][2]["emdash_task_id"] == "sid-1"
    statuses = [e["payload"]["status"] for c in client.of("post_events") for e in c[1][1]
                if e["kind"] == "status"]
    assert statuses == ["created_session", "submitted", "needs_input"]
    assert client.of("finish")[0][1] == ("turn-0001-aaaa", "all done")
    assert client.of("finish")[0][2] == {"status": "done", "emdash_task_id": "sid-1"}
    assert desktop.is_desktop_session(cfg, "sid-1")


def test_an_agent_turn_finishes_once_delivered(cfg, tmp_path, no_app):
    cfg.desktop_projects = {"hal": str(_repo(tmp_path))}
    turn = _turn(project="", agent_slug="hal", prompt="")
    client = FakeClient()
    run = desktop.TurnRun(cfg, client, "r", turn, "th", {"reuse": False}, reuse="")
    th = threading.Thread(target=run.run)
    th.start()
    ch = _channel_of(cfg, "hal")
    _mod(ch, "task", "sid-1", "never read")
    th.join(10)
    assert (ch / "task.txt").read_text() == "/hal:turn"
    fin = client.of("finish")[0]
    assert fin[2]["status"] == "done" and "delivered" in fin[1][1]


def test_a_followup_wakes_a_stopped_session(cfg, tmp_path, no_app):
    wt = tmp_path / "wt"
    (wt / desktop.CHANNEL).mkdir(parents=True)
    (wt / desktop.CHANNEL / "fu-1.txt").write_text("earlier")
    desktop._remember(cfg, "sid-9", wt, "scratch")
    turn = _turn(origin_ref={"thread_key": "th", "chat_session_id": "c"}, prompt="again")
    client = FakeClient()
    run = desktop.TurnRun(cfg, client, "r", turn, "th", {"reuse": True}, reuse="sid-9")
    th = threading.Thread(target=run.run)
    th.start()
    _mod(wt / desktop.CHANNEL, "fu-2", "sid-9", "second")
    th.join(10)
    assert (wt / desktop.CHANNEL / "fu-2.txt").read_text() == "again"
    assert no_app == ["sid-9"]  # no `alive` stamp -> woken with the deep link
    assert not client.of("record_session")
    assert client.of("finish")[0][1][1] == "second"


# ── stopping ────────────────────────────────────────────────────────────────

def _live_channel(cfg, tmp_path, sid="sid-5") -> Path:
    wt = tmp_path / "wt-stop"
    ch = wt / desktop.CHANNEL
    ch.mkdir(parents=True)
    (ch / "alive").write_text(str(int(time.time() * 1000)))
    desktop._remember(cfg, sid, wt, "scratch")
    return ch


def _mod_stops(ch: Path, outcome: str):
    for _ in range(300):
        if (ch / "stop-1.txt").exists():
            break
        time.sleep(0.02)
    t = int(time.time() * 1000)
    (ch / f"ev-{t}-1-stopped.json").write_text(json.dumps(
        {"t": t, "kind": "stopped", "extra": {"which": "stop-1", "outcome": outcome}}))


def test_stop_asks_the_mod_and_reports_its_verdict(cfg, tmp_path):
    ch = _live_channel(cfg, tmp_path)
    th = threading.Thread(target=_mod_stops, args=(ch, "interrupted"))
    th.start()
    assert desktop.stop(cfg, "sid-5", wait=5)["action"] == "interrupted"
    th.join()


def test_stopping_a_session_with_no_process_is_idle(cfg, tmp_path):
    ch = _live_channel(cfg, tmp_path)
    (ch / "alive").write_text("0")
    assert desktop.stop(cfg, "sid-5")["action"] == "idle"
    assert not (ch / "stop-1.txt").exists()


def test_an_unanswered_stop_is_unreadable_not_success(cfg, tmp_path):
    _live_channel(cfg, tmp_path)
    assert desktop.stop(cfg, "sid-5", wait=0.5)["action"] == "unreadable"


def test_a_cancelled_chat_turn_stops_the_session_and_finishes_cancelled(cfg, tmp_path, no_app, monkeypatch):
    ch = _live_channel(cfg, tmp_path)
    (ch / "seeded").write_text("sid-5")
    monkeypatch.setattr(desktop, "stop", lambda c, sid, **k: {"action": "interrupted"})
    turn = _turn(id="turn-cancel-1", origin_ref={"thread_key": "th", "chat_session_id": "c"})
    client = FakeClient()
    run = desktop.TurnRun(cfg, client, "r", turn, "th", {"reuse": True}, reuse="sid-5")
    t = int(time.time() * 1000)
    (ch / "fu-1.txt").write_text("x")
    (ch / f"ev-{t}-1-submitted.json").write_text(json.dumps(
        {"t": t, "kind": "submitted", "extra": {"which": "fu-2"}}))
    desktop.CANCELLED_TURNS.add("turn-cancel-1")
    run.run()
    fin = client.of("finish")[0]
    assert fin[1] == ("turn-cancel-1", "cancelled by user")
    assert fin[2] == {"status": "cancelled", "emdash_task_id": "sid-5"}
    assert "turn-cancel-1" not in desktop.CANCELLED_TURNS


# ── finding the CLI under launchd's bare PATH ───────────────────────────────

def _no_cli_anywhere(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")  # what launchd gives the runner
    monkeypatch.setattr(desktop.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    real_is_file = desktop.Path.is_file
    monkeypatch.setattr(desktop.Path, "is_file",
                        lambda self: str(self).startswith(str(tmp_path)) and real_is_file(self))


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_the_cli_is_found_in_local_bin_without_it_on_path(monkeypatch, tmp_path):
    _no_cli_anywhere(monkeypatch, tmp_path)
    cli = _exe(tmp_path / ".local" / "bin" / "claude")
    assert desktop.claude_cli() == cli


def test_the_apps_bundled_cli_is_the_last_resort(monkeypatch, tmp_path):
    _no_cli_anywhere(monkeypatch, tmp_path)
    bundled = _exe(tmp_path / "Library" / "Application Support" / "Claude" / "claude-code"
                   / "2.1.288" / "claude.app" / "Contents" / "MacOS" / "claude")
    assert desktop.claude_cli() == bundled


def test_no_cli_means_new_threads_stay_on_emdash_and_readiness_says_why(cfg, monkeypatch, tmp_path):
    _no_cli_anywhere(monkeypatch, tmp_path)
    assert desktop.claude_cli() is None
    monkeypatch.setattr(desktop, "_current", desktop.CLAUDE_DESKTOP)
    assert desktop.maybe_execute(cfg, FakeClient(), "r", _turn(), "th") is None

    from canopy_runner import readiness
    monkeypatch.setattr(readiness.cdp_control, "cdp_healthy", lambda port: True)
    ready, note = readiness.compute(SimpleNamespace(cdp_port=1, state_path=str(tmp_path / "state")))
    assert ready is False and "no Claude Code CLI" in note


def test_the_cli_runs_with_a_real_path(tmp_path):
    env = desktop._cli_env(Path("/somewhere/bin/claude"))
    assert env["PATH"].split(":")[:2] == ["/somewhere/bin", str(Path.home() / ".local" / "bin")]
