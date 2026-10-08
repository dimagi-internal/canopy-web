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

#: The real resolver, for the tests about finding the CLI. Every other test runs
#: with a stub, so they pass on a box with no Claude Code CLI at all — CI has none.
REAL_CLAUDE_CLI = desktop.claude_cli


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
    monkeypatch.setattr(desktop, "claude_cli", lambda: Path("/stub/bin/claude"))
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
    monkeypatch.setattr(desktop, "seed_session",
                        lambda cfg, wt, name="": SEEDED_NAMES.append(name) or "sid-1")
    monkeypatch.setattr(desktop.caller, "write_caller_file", lambda turn: None)
    SEEDED_NAMES.clear()
    return opened


#: The title each seeded session was given, in order (the no_app fixture's seed stub).
SEEDED_NAMES: list[str] = []


def test_seed_names_the_session_so_the_app_titles_it(cfg, tmp_path, monkeypatch):
    """`--name` is what the app reads for the sidebar title; without it every
    runner session showed up as "General coding session"."""
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(stdout=json.dumps({"session_id": "sid-7"}), stderr="")

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)
    assert desktop.seed_session(cfg, tmp_path, "c-fix-the-login-ab12") == "sid-7"
    i = seen["argv"].index("--name")
    assert seen["argv"][i + 1] == "c-fix-the-login-ab12"
    desktop.seed_session(cfg, tmp_path)
    assert "--name" not in seen["argv"]


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
    # Titled like emdash names it: the canopy prefix, then a subject from the prompt.
    assert SEEDED_NAMES and SEEDED_NAMES[0].startswith("c-do-it")


def test_a_chat_turns_attachments_reach_the_desktop_session(cfg, tmp_path, monkeypatch):
    """A screenshot sent from the web was dropped on this runtime: the session got
    the words alone (live 2026-10-06, a supervisor request whose image never
    arrived). It must be downloaded and named in the prompt, as on emdash."""
    from canopy_runner import execute

    monkeypatch.setattr(execute, "ATTACHMENT_ROOT", tmp_path / "att")

    class Client(FakeClient):
        def download_attachment(self, aid, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"png")

    turn = _turn(prompt="see the screenshot", origin_ref={
        "thread_key": "th", "chat_session_id": "c",
        "attachments": [{"id": "a1", "filename": "dialog.png"}]})
    run = desktop.TurnRun(cfg, Client(), "r", turn, "th", {"reuse": False}, reuse="")
    prompt = run._prompt()
    assert prompt.startswith("see the screenshot")
    shot = tmp_path / "att" / "turn-0001-aaaa" / "dialog.png"
    assert str(shot) in prompt and shot.read_bytes() == b"png"


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
    monkeypatch.setattr(desktop, "claude_cli", REAL_CLAUDE_CLI)  # the thing under test here
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


# ── the session report ──────────────────────────────────────────────────────

def test_desktop_sessions_join_the_session_report(cfg, tmp_path, monkeypatch):
    """Left out of the report, canopy-web listed every desktop session as archived."""
    live = tmp_path / "wt-live"
    ch = live / desktop.CHANNEL
    ch.mkdir(parents=True)
    (ch / "alive").write_text(str(int(time.time() * 1000)))
    t = int(time.time() * 1000)
    (ch / f"ev-{t}-1-turn.start.json").write_text(json.dumps({"t": t, "kind": "turn.start"}))
    desktop._remember(cfg, "sid-live", live, "hal")
    desktop._remember(cfg, "sid-gone", tmp_path / "deleted-worktree", "hal")
    monkeypatch.setattr(desktop, "transcript_path", lambda sid, claude_home=None: None)
    rows = desktop.open_sessions(cfg)
    assert [r["emdash_task"] for r in rows] == ["sid-live"]  # a gone worktree is a closed session
    assert rows[0]["project"] == "hal" and rows[0]["agent_status"] == "working"
    t2 = t + 5
    (ch / f"ev-{t2}-2-ask.json").write_text(json.dumps({"t": t2, "kind": "ask", "extra": {"tool": "Bash"}}))
    assert desktop.open_sessions(cfg)[0]["agent_status"] == "awaiting-input"
    (ch / f"ev-{t2 + 1}-3-turn.complete.json").write_text(json.dumps({"t": t2 + 1, "kind": "turn.complete"}))
    assert desktop.open_sessions(cfg)[0]["agent_status"] == ""


def test_a_new_sessions_turn_transcript_skips_the_seed(cfg, tmp_path, no_app, monkeypatch):
    cfg.desktop_projects = {"scratch": str(_repo(tmp_path))}
    seed_file = tmp_path / "proj" / "sid-1.jsonl"
    seed_file.parent.mkdir()
    seed_file.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "ready"}]}}) + "\n")
    monkeypatch.setattr(desktop, "transcript_path", lambda sid, claude_home=None: seed_file)
    turn = _turn(origin_ref={"thread_key": "th", "chat_session_id": "c"})
    client = FakeClient()
    run = desktop.TurnRun(cfg, client, "r", turn, "th", {"reuse": False}, reuse="")
    th = threading.Thread(target=run.run)
    th.start()
    ch = _channel_of(cfg, "scratch")
    with seed_file.open("a") as fh:  # the real work, after the seed
        fh.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "the work"}]}}) + "\n")
    _mod(ch, "task", "sid-1", "done")
    th.join(10)
    shipped = [ln for c in client.of("post_transcript") for ln in c[1][1]]
    assert shipped and all("ready" not in ln for ln in shipped)
    assert any("the work" in ln for ln in shipped)


# ── the shells a stopped turn leaves behind ─────────────────────────────────

def test_only_the_stopped_turns_shells_are_selected():
    now = time.time()
    snap = "/bin/zsh -c source /Users/x/.claude/shell-snapshots/snapshot-zsh-1.sh && eval 'python3 -c sleep'"
    procs = [
        (10, 1, now - 300, "/Applications/Claude.app/Contents/Helpers/disclaimer --pgroup -- claude --resume=sid-1"),
        (11, 10, now - 300, "claude --output-format stream-json --resume=sid-1"),
        (20, 11, now - 5, snap),              # this turn's Bash        -> killed
        (21, 20, now - 5, "python3 -c sleep"),  # ...and its child       -> killed
        (30, 11, now - 200, snap),            # an earlier background task -> kept
        (40, 11, now - 5, "node mcp-server.js"),  # an MCP server        -> kept
        (50, 99, now - 5, snap),              # another session's shell -> kept
    ]
    assert sorted(desktop.turn_shells("sid-1", now - 30, procs=procs)) == [20, 21]


def test_etime_parses_every_ps_shape():
    assert desktop._etime_seconds("00:22") == 22
    assert desktop._etime_seconds("01:02:03") == 3723
    assert desktop._etime_seconds("2-00:00:01") == 172801


def test_the_same_name_twice_gets_two_worktrees(cfg, tmp_path):
    repo = _repo(tmp_path)
    a = desktop.make_worktree(cfg, repo, "HEAD", "c-same-name-e3b9")
    b = desktop.make_worktree(cfg, repo, "HEAD", "c-same-name-e3b9")
    assert a != b and a.exists() and b.exists()


def test_a_stop_kills_the_running_turns_shells_before_aborting(cfg, tmp_path, monkeypatch):
    """Kill-then-abort: killing after the abort hits a BACKGROUNDED task, whose death
    makes Claude Code start a new turn that redoes the work."""
    ch = _live_channel(cfg, tmp_path)
    t = int(time.time() * 1000)
    (ch / f"ev-{t}-1-turn.start.json").write_text(json.dumps({"t": t, "kind": "turn.start"}))
    order = []
    monkeypatch.setattr(desktop, "kill_turn_shells",
                        lambda sid, since: order.append(("kill", not (ch / "stop-1.txt").exists())) or 2)
    th = threading.Thread(target=_mod_stops, args=(ch, "interrupted"))
    th.start()
    res = desktop.stop(cfg, "sid-5", wait=5)
    th.join()
    assert order == [("kill", True)]  # killed while no stop request existed yet
    assert res == {"action": "interrupted", "reason": "", "killed": 2}


# ── the seed stays out of every session view ────────────────────────────────

def test_drop_seed_removes_only_the_seed_exchange():
    rows = [
        {"index": 1, "role": "user", "text": desktop.SEED_PROMPT},
        {"index": 2, "role": "assistant", "text": "ready"},
        {"index": 3, "role": "user", "text": "the real task"},
        {"index": 4, "role": "assistant", "text": "ready"},  # a real reply that says ready: kept
    ]
    assert [r["index"] for r in desktop.drop_seed(rows)] == [3, 4]
    assert desktop.drop_seed([{"role": "user", "text": "hello"}]) == [{"role": "user", "text": "hello"}]


def test_a_new_desktop_session_is_recorded_with_its_task_name(cfg, tmp_path, no_app):
    cfg.desktop_projects = {"scratch": str(_repo(tmp_path))}
    turn = _turn(origin_ref={"thread_key": "th", "chat_session_id": "c"})
    client = FakeClient()
    run = desktop.TurnRun(cfg, client, "r", turn, "th", {"reuse": False}, reuse="")
    th = threading.Thread(target=run.run)
    th.start()
    _mod(_channel_of(cfg, "scratch"), "task", "sid-1", "done")
    th.join(10)
    title = client.of("record_session")[0][2]["title"]
    assert title and title != "sid-1"


# ── opening a session gives focus back (canopy-web#1188, Focus round 2) ──────

def _record_runs(monkeypatch, results):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        r = results.pop(0) if results else subprocess.CompletedProcess(argv, 0, "", "")
        if isinstance(r, BaseException):
            raise r
        return r
    monkeypatch.setattr(desktop.subprocess, "run", run)
    return calls


def test_opening_a_session_goes_through_the_focus_returning_helper(monkeypatch):
    calls = _record_runs(monkeypatch, [subprocess.CompletedProcess([], 0, "gave focus back", "")])
    desktop.open_session("sid-9")
    assert calls == [["osascript", "-l", "JavaScript", str(desktop.QUIET_OPEN),
                      "claude://resume?session=sid-9", str(desktop.QUIET_OPEN_WATCH_SECONDS)]]
    assert desktop.QUIET_OPEN.is_file()  # shipped next to the module, so in the wheel


def test_a_failed_helper_still_opens_the_session(monkeypatch):
    calls = _record_runs(monkeypatch, [subprocess.CompletedProcess([], 1, "", "execution error")])
    desktop.open_session("sid-9")
    assert calls[-1] == ["open", "-g", "claude://resume?session=sid-9"]


def test_no_osascript_still_opens_the_session(monkeypatch):
    calls = _record_runs(monkeypatch, [FileNotFoundError("osascript")])
    desktop.open_session("sid-9")
    assert calls[-1] == ["open", "-g", "claude://resume?session=sid-9"]


def test_a_helper_that_timed_out_does_not_open_twice(monkeypatch):
    calls = _record_runs(monkeypatch, [subprocess.TimeoutExpired("osascript", 18)])
    desktop.open_session("sid-9")
    assert len(calls) == 1


# ── closing a session from the web ──────────────────────────────────────────

def _desktop_session(cfg, tmp_path, sid="sid-c1"):
    repo = _repo(tmp_path)
    wt = desktop.make_worktree(cfg, repo, "HEAD", "c-close-me-ab12")
    desktop._remember(cfg, sid, wt, "scratch")
    return repo, wt


def test_close_retires_a_desktop_session_and_keeps_its_branch(cfg, tmp_path, monkeypatch):
    """Close used to look the id up in emdash, find nothing, call it 'already
    gone' — and the session kept running and came back on the next report."""
    from canopy_runner import close as close_mod, sessions

    monkeypatch.setattr(sessions, "_PENDING_CLOSED", set())
    repo, wt = _desktop_session(cfg, tmp_path)
    assert [r["emdash_task"] for r in desktop.open_sessions(cfg)] == ["sid-c1"]

    assert close_mod.close_session("sid-c1", cfg=cfg, emdash_db="/nonexistent") == "closed"
    assert desktop.open_sessions(cfg) == []          # no longer reported as open
    assert not desktop.is_desktop_session(cfg, "sid-c1")
    assert desktop.worktree_for(cfg, "sid-c1") is None
    assert "sid-c1" in sessions._PENDING_CLOSED      # canopy is told on the next report
    assert not wt.exists()                           # clean worktree: removed
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "canopy-desktop/*"],
                              capture_output=True, text=True).stdout
    assert "c-close-me-ab12" in branches             # but the branch is kept
    assert desktop.close(cfg, "sid-c1") == "absent"  # a double tap is a no-op


def test_close_keeps_a_worktree_holding_uncommitted_work(cfg, tmp_path):
    _, wt = _desktop_session(cfg, tmp_path)
    (wt / "notes.txt").write_text("not committed")
    assert desktop.close(cfg, "sid-c1") == "closed"
    assert (wt / "notes.txt").read_text() == "not committed"
    assert desktop.open_sessions(cfg) == []


def test_a_turn_on_a_closed_sessions_thread_opens_a_new_desktop_session(cfg, tmp_path, monkeypatch):
    _desktop_session(cfg, tmp_path)
    desktop.close(cfg, "sid-c1")
    monkeypatch.setattr(desktop, "_current", desktop.CLAUDE_DESKTOP)
    started = []
    monkeypatch.setattr(desktop.TurnRun, "run", lambda self: started.append(self.reuse))
    client = FakeClient(plan={"reuse": True, "emdash_task_id": "sid-c1"})
    action = desktop.maybe_execute(cfg, client, "r", _turn(), "th")
    assert action and action.startswith("desktop:create:")  # not handed to emdash
    for th in list(desktop.IN_FLIGHT.values()):
        th.join(5)
    assert started == [""]


def test_closing_a_desktop_session_drops_it_from_the_report(cfg, tmp_path, monkeypatch):
    """There is no emdash task to delete, so the old close found none, said "already
    gone", and the next report named the session open again — retried every tick,
    forever, with the session stuck on the feed (2026-10-07)."""
    from canopy_runner import close, sessions

    wt = tmp_path / "wt"
    wt.mkdir()
    desktop._remember(cfg, "sid-close", wt, "hal")
    monkeypatch.setattr(desktop, "transcript_path", lambda sid, claude_home=None: None)
    monkeypatch.setattr(close.cdp_control, "close_task",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no emdash delete")))
    sessions._PENDING_CLOSED.clear()
    assert [r["emdash_task"] for r in desktop.open_sessions(cfg)] == ["sid-close"]

    assert close.close_session("sid-close", project="hal", cfg=cfg) == "closed"
    assert desktop.open_sessions(cfg) == []
    assert sessions._PENDING_CLOSED == {"sid-close"}
    assert wt.exists()  # not a git checkout git can vouch for: never removed


# ── the app's feature gates (canopy-web#1238) ────────────────────────────────

def _write_fcache(path: Path, features: dict, ts_ms: int = 1_791_000_000_000) -> Path:
    import gzip

    body = gzip.compress(json.dumps({"timestamp": ts_ms, "mode": "1p", "orgUuid": "o",
                                     "features": features}).encode())
    path.write_bytes(desktop.FCACHE_MAGIC + body)
    return path


def test_app_features_reads_both_gates_as_the_app_caches_them(tmp_path):
    # The shape observed on haldimagi 2026-10-07 (Claude 2.26454.2): start_session
    # off by default, the idle timeout forced to 0.
    p = _write_fcache(tmp_path / "fcache", {
        desktop.GATE_START_SESSION: {"value": False, "on": False, "off": True, "source": "defaultValue"},
        desktop.GATE_IDLE_TIMEOUT: {"value": 0, "on": False, "off": True, "source": "force"},
    })
    f = desktop.app_features(p)
    assert f == {"fetched_at": 1_791_000_000, "start_session": False, "idle_timeout": 0}
    line = desktop.describe_app_features(f)
    assert "start_session off" in line and "idle timeout off" in line


def test_app_features_says_when_start_session_turns_on(tmp_path):
    p = _write_fcache(tmp_path / "fcache", {
        desktop.GATE_START_SESSION: {"value": True, "on": True},
        desktop.GATE_IDLE_TIMEOUT: {"value": 900, "on": True},
    })
    line = desktop.describe_app_features(desktop.app_features(p))
    assert "start_session ON" in line and "#1238" in line and "idle timeout 900s" in line


def test_app_features_missing_gates_are_unknown_not_off(tmp_path):
    f = desktop.app_features(_write_fcache(tmp_path / "fcache", {}))
    assert f["start_session"] is None and f["idle_timeout"] is None
    assert "not in cache" in desktop.describe_app_features(f)


@pytest.mark.parametrize("content", [None, b"not a cache", desktop.FCACHE_MAGIC + b"not gzip"])
def test_app_features_unreadable_cache_is_none(tmp_path, content):
    p = tmp_path / "fcache"
    if content is not None:
        p.write_bytes(content)
    assert desktop.app_features(p) is None
    assert "unknown" in desktop.describe_app_features(None)
