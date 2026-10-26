"""The `claude-cli` session runtime (desktop.py + headless.py): the runner runs Claude
itself, headless over ACP, and never opens the Claude app.

The executor is stubbed here (CI has no Claude). What these pin is the routing:
a new thread runs headless, a follow-up runs headless while the app does not hold
the session and goes through the mod while it does, and nothing ever opens a
claude:// link. The live half — a real adapter, a real session resumed, a turn
stopped mid-flight — was run against real Claude on a laptop (harness in the PR).
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from canopy_runner import desktop, headless
from canopy_runner.cancel import CANCELLED_TURNS

from .test_desktop import FakeClient, _repo, _turn


@pytest.fixture(autouse=True)
def direct_runtime(monkeypatch):
    monkeypatch.setattr(desktop, "_current", desktop.CLAUDE_CLI)
    monkeypatch.setattr(desktop, "IN_FLIGHT", {})
    monkeypatch.setattr(desktop, "claude_cli", lambda: Path("/stub/bin/claude"))
    monkeypatch.setattr(desktop, "prepare_settings", lambda cfg, wt, **kw: None)
    monkeypatch.setattr(desktop.caller, "write_caller_file", lambda turn: None)
    monkeypatch.setattr(desktop, "transcript_path", lambda sid, claude_home=None: None)
    monkeypatch.setattr(headless, "adapter_argv", lambda: ["/stub/adapter"])
    monkeypatch.setattr(desktop, "ensure_app",
                        lambda: pytest.fail("the direct runtime must never start the app"))
    monkeypatch.setattr(desktop, "open_session",
                        lambda sid: pytest.fail("the direct runtime must never open a deep link"))
    yield


#: The env each stubbed headless turn was started with, in order.
ENVS: list[dict] = []


@pytest.fixture
def runs(monkeypatch):
    ENVS.clear()
    """Stub the headless executor; record each call."""
    calls: list[dict] = []

    def run_turn(prompt, *, cwd, emit, env=None, resume=None, on_session=None,
                 cancelled=lambda: False, timeout=0):
        sid = resume or "sid-new"
        if on_session:
            on_session(sid)
        emit([{"kind": "assistant", "payload": {"text": "hi"}}])
        calls.append({"prompt": prompt, "cwd": cwd, "resume": resume})
        ENVS.append(env or {})
        return True, "answer", sid

    monkeypatch.setattr(headless, "run_turn", run_turn)
    return calls


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(desktop_dir=str(tmp_path / "desktop"), emdash_db="",
                           project_paths={"scratch": str(_repo(tmp_path))},
                           desktop_allow=None, desktop_model="")


def _go(cfg, client, turn):
    desktop.maybe_execute(cfg, client, "rid", turn, "th")
    desktop.IN_FLIGHT[str(turn["id"])].join(timeout=10)


def test_a_new_thread_runs_headless_and_is_indexed_as_direct(cfg, runs):
    client = FakeClient()
    _go(cfg, client, _turn())
    assert runs[0]["resume"] is None
    entry = desktop._index(cfg)["sid-new"]
    assert entry["runtime"] == desktop.CLAUDE_CLI
    # The mod claims the channel by this id once the app holds the session.
    assert (Path(entry["worktree"]) / desktop.CHANNEL / "seeded").read_text() == "sid-new"
    finish = client.of("finish")[0]
    assert finish[2]["status"] == "done" and finish[2]["emdash_task_id"] == "sid-new"
    assert client.of("record_session")[0][2]["session_id"] == "sid-new"
    # An agent/project turn runs to completion: there is no visible session to
    # carry it on, unlike emdash.
    assert finish[1][1] == "answer"


def test_a_followup_the_app_does_not_hold_resumes_headless(cfg, runs):
    _go(cfg, FakeClient(), _turn())
    _go(cfg, FakeClient(plan={"reuse": True, "emdash_task_id": "sid-new"}), _turn(id="turn-2"))
    assert [r["resume"] for r in runs] == [None, "sid-new"]


def test_a_followup_the_app_holds_goes_through_the_mod(cfg, runs, monkeypatch):
    _go(cfg, FakeClient(), _turn())
    channel = Path(desktop._index(cfg)["sid-new"]["worktree"]) / desktop.CHANNEL
    (channel / "alive").write_text(str(int(time.time() * 1000)))
    monkeypatch.setattr(desktop, "SUBMIT_TIMEOUT_SECONDS", 0.2)
    client = FakeClient(plan={"reuse": True, "emdash_task_id": "sid-new"})
    _go(cfg, client, _turn(id="turn-2", prompt="from canopy"))
    assert (channel / "fu-1.txt").read_text() == "from canopy"
    assert len(runs) == 1  # not run headless: the app's process owns the session


def test_an_agent_turn_into_the_app_waits_for_its_completion(cfg, runs, monkeypatch):
    _go(cfg, FakeClient(), _turn())
    channel = Path(desktop._index(cfg)["sid-new"]["worktree"]) / desktop.CHANNEL
    (channel / "alive").write_text(str(int(time.time() * 1000)))
    t = int(time.time() * 1000) + 5000
    for i, (kind, extra) in enumerate([("submitted", {"which": "fu-1"}),
                                       ("turn.complete", {"answer": "from the app"})]):
        (channel / f"ev-{t + i}-{i}-{kind}.json").write_text(
            f'{{"t": {t + i}, "sid": "sid-new", "kind": "{kind}", "extra": {__import__("json").dumps(extra)}}}')
    client = FakeClient(plan={"reuse": True, "emdash_task_id": "sid-new"})
    _go(cfg, client, _turn(id="turn-2"))
    finish = client.of("finish")[0]
    assert finish[2]["status"] == "done" and finish[1][1] == "from the app"


def test_no_adapter_means_new_threads_stay_on_emdash(cfg, runs, monkeypatch):
    monkeypatch.setattr(headless, "adapter_argv", lambda: None)
    assert desktop.maybe_execute(cfg, FakeClient(), "rid", _turn(), "th") is None


def test_a_callers_confined_turn_never_runs_direct(cfg, runs, monkeypatch):
    monkeypatch.setattr(desktop.caller, "capability", lambda turn: {"name": "ask"})
    assert desktop.maybe_execute(cfg, FakeClient(), "rid", _turn(), "th") is None


def test_a_cancelled_headless_turn_finishes_cancelled(cfg, monkeypatch):
    def run_turn(prompt, *, cwd, emit, env=None, resume=None, on_session=None,
                 cancelled=lambda: False, timeout=0):
        on_session("sid-new")
        assert cancelled()
        return False, "cancelled", "sid-new"

    monkeypatch.setattr(headless, "run_turn", run_turn)
    CANCELLED_TURNS.add("turn-0001-aaaa")
    client = FakeClient()
    _go(cfg, client, _turn())
    assert client.of("finish")[0][2]["status"] == "cancelled"
    assert "turn-0001-aaaa" not in CANCELLED_TURNS


def test_a_failed_headless_turn_names_its_session(cfg, monkeypatch):
    def run_turn(prompt, *, cwd, emit, env=None, resume=None, on_session=None, **kw):
        raise RuntimeError("adapter gone")

    monkeypatch.setattr(headless, "run_turn", run_turn)
    _go(cfg, FakeClient(), _turn())  # no session yet: a plain failure
    client = FakeClient(plan={"reuse": True, "emdash_task_id": "sid-x"})
    desktop._remember(cfg, "sid-x", Path(cfg.desktop_dir), "scratch", runtime=desktop.CLAUDE_CLI)
    _go(cfg, client, _turn(id="turn-2"))
    finish = client.of("finish")[0]
    assert finish[2]["status"] == "failed" and finish[2]["emdash_task_id"] == "sid-x"
    assert "adapter gone" in finish[1][1]


def test_stop_cancels_a_headless_turn_and_the_report_says_working(cfg, tmp_path, monkeypatch):
    cancelled = []
    agent = SimpleNamespace(cancel=lambda: cancelled.append(True))
    monkeypatch.setitem(headless.RUNNING, "sid-run", agent)
    desktop._remember(cfg, "sid-run", tmp_path, "scratch", runtime=desktop.CLAUDE_CLI)
    assert desktop.open_sessions(cfg)[0]["agent_status"] == "working"
    assert desktop.stop(cfg, "sid-run")["action"] == "interrupted"
    assert cancelled == [True]


def test_the_mod_never_claims_a_headless_session():
    mod = (Path(desktop.MOD_SRC) / "mod" / "hooks" / "register.tsx").read_text()
    assert f"$.env.get('{headless.HEADLESS_ENV}')" in mod


def test_observe_direct_installs_the_mod_and_the_adapter(monkeypatch):
    seen = []
    monkeypatch.setattr(desktop, "_current", desktop.EMDASH)
    monkeypatch.setattr(desktop, "ensure_mod", lambda: seen.append("mod"))
    monkeypatch.setattr(headless, "ensure_adapter", lambda: seen.append("adapter"))
    desktop.observe({"engine": "claude-cli"})
    deadline = time.time() + 5
    while "adapter" not in seen and time.time() < deadline:
        time.sleep(0.01)
    assert desktop.current() == desktop.CLAUDE_CLI and seen == ["mod", "adapter"]


def test_a_resume_that_falls_back_moves_the_thread_to_the_new_session(cfg, monkeypatch):
    def run_turn(prompt, *, cwd, emit, env=None, resume=None, on_session=None, **kw):
        on_session("sid-fresh")  # the shared driver could not load `resume`
        return True, "answer", "sid-fresh"

    wt = Path(cfg.desktop_dir) / "wt"
    (wt / desktop.CHANNEL).mkdir(parents=True)
    desktop._remember(cfg, "sid-old", wt, "scratch", runtime=desktop.CLAUDE_CLI)
    monkeypatch.setattr(headless, "run_turn", run_turn)
    client = FakeClient(plan={"reuse": True, "emdash_task_id": "sid-old"})
    _go(cfg, client, _turn(id="turn-2"))
    assert desktop.is_cli_session(cfg, "sid-fresh")
    assert (wt / desktop.CHANNEL / "seeded").read_text() == "sid-fresh"
    assert client.of("record_session")[0][2]["session_id"] == "sid-fresh"
    assert client.of("finish")[0][2]["emdash_task_id"] == "sid-fresh"


# ── a box with no emdash (Windows, or a laptop that never installed it) ─────

def test_claude_cli_is_ready_with_emdash_down(cfg, tmp_path, monkeypatch):
    from canopy_runner import readiness

    monkeypatch.setattr(readiness.cdp_control, "cdp_healthy",
                        lambda **kw: pytest.fail("claude-cli must not ask emdash"))
    cfg.state_path = str(tmp_path / "runner-state.json")
    assert readiness.compute(cfg) == (True, "")


def test_claude_cli_without_its_adapter_says_how_to_install_it(cfg, tmp_path, monkeypatch):
    from canopy_runner import readiness

    monkeypatch.setattr(headless, "adapter_argv", lambda: None)
    cfg.state_path = str(tmp_path / "runner-state.json")
    ready, note = readiness.compute(cfg)
    assert not ready and "claude-agent-acp" in note and "runtime set claude-cli" in note


def test_emdash_runtimes_still_gate_on_emdash(monkeypatch):
    for runtime in (desktop.EMDASH, desktop.CLAUDE_DESKTOP):
        monkeypatch.setattr(desktop, "_current", runtime)
        assert desktop.needs_emdash()
    monkeypatch.setattr(desktop, "_current", desktop.CLAUDE_CLI)
    assert not desktop.needs_emdash()


def test_a_box_with_no_emdash_reports_its_own_projects(cfg):
    from canopy_runner import sessions

    cfg.emdash_db = ""  # Path("") is ".", which exists: it must not read as a DB
    assert sessions.reported_projects(cfg) == ["scratch"]


def test_projects_add_reaches_the_running_daemon(cfg, tmp_path):
    import json

    from canopy_runner import sessions

    conf = tmp_path / "runner.json"
    conf.write_text(json.dumps({"token": "t", "project_paths": {}}))
    cfg.config_path, cfg.emdash_db = str(conf), ""
    assert sessions.reported_projects(cfg) == []
    (tmp_path / "other").mkdir()
    repo = _repo(tmp_path / "other")
    desktop.set_project_path(str(conf), "other", str(repo))
    assert sessions.reported_projects(cfg) == ["other"]
    assert desktop.project_repo(cfg, "other") == (repo, "HEAD")
    desktop.set_project_path(str(conf), "other", None)
    assert sessions.reported_projects(cfg) == []
    assert json.loads(conf.read_text())["token"] == "t"  # the rest of the file is kept


def test_emdash_drives_only_what_emdash_holds(cfg, monkeypatch):
    from canopy_runner import sessions

    monkeypatch.setattr(desktop, "_current", desktop.EMDASH)
    cfg.emdash_db = ""
    assert sessions.reported_projects(cfg) == []


def test_drain_one_claims_on_claude_cli_with_emdash_down(cfg, tmp_path, monkeypatch):
    from canopy_runner import main as main_mod

    monkeypatch.setattr("canopy_runner.cdp_control.cdp_healthy",
                        lambda **k: pytest.fail("claude-cli must not ask emdash"))
    monkeypatch.setattr("canopy_runner.cdp_control.host_id", lambda: "u@h")
    monkeypatch.setattr(main_mod, "_paused_agents", lambda c: set())
    monkeypatch.setattr(main_mod, "_claim_and_execute", lambda c, client, paused: "claimed")
    monkeypatch.setattr(main_mod.chat_pump, "drain_chat_bridges", lambda c, client: None)
    beats = []
    client = SimpleNamespace(heartbeat=lambda *a, **k: beats.append(k))
    cfg.state_path, cfg.runner_id, cfg.cdp_port = str(tmp_path / "s.json"), "rid", 9222
    assert main_mod.drain_one(cfg, client) == "claimed"
    assert beats[0]["ready"] is True and beats[0]["projects"] == ["scratch"]


# ── forking: a new session cut from another's transcript, in the same worktree ──

def _source_with_transcript(cfg, runs, tmp_path, monkeypatch, records=6):
    """A Claude CLI session `sid-new` whose transcript has `records` records."""
    import json

    _go(cfg, FakeClient(), _turn())
    project_dir = tmp_path / "claude-projects" / "-wt"
    project_dir.mkdir(parents=True)
    src = project_dir / "sid-new.jsonl"
    src.write_text("".join(json.dumps({"sessionId": "sid-new", "n": i}) + "\n" for i in range(records))
                   + "not json\n")
    monkeypatch.setattr(desktop, "transcript_path",
                        lambda sid, claude_home=None: p if (p := project_dir / f"{sid}.jsonl").exists() else None)
    return project_dir


def test_a_forks_first_turn_cuts_the_transcript_and_resumes_the_cut(cfg, runs, tmp_path, monkeypatch):
    import json

    project_dir = _source_with_transcript(cfg, runs, tmp_path, monkeypatch)
    client = FakeClient(plan={"reuse": False, "fork": {"session_key": "sid-new", "records": 4}})

    _go(cfg, client, _turn(id="turn-2", prompt="try it the other way"))

    fork_sid = client.of("record_session")[0][2]["session_id"]
    assert fork_sid != "sid-new"
    cut = [json.loads(line) for line in (project_dir / f"{fork_sid}.jsonl").read_text().splitlines()]
    assert [r["n"] for r in cut] == [0, 1, 2, 3]
    assert {r["sessionId"] for r in cut} == {fork_sid}
    assert runs[-1] == {"prompt": "try it the other way", "cwd": runs[0]["cwd"], "resume": fork_sid}
    index = desktop._index(cfg)
    assert index[fork_sid]["worktree"] == index["sid-new"]["worktree"]
    assert index[fork_sid]["runtime"] == desktop.CLAUDE_CLI
    # The source keeps its transcript and its claim on the app channel.
    assert len((project_dir / "sid-new.jsonl").read_text().splitlines()) == 7
    channel = Path(index["sid-new"]["worktree"]) / desktop.CHANNEL
    assert (channel / "seeded").read_text() == "sid-new"


def test_a_fork_that_cannot_be_cut_fails_its_turn(cfg, runs, tmp_path, monkeypatch):
    _source_with_transcript(cfg, runs, tmp_path, monkeypatch, records=2)
    client = FakeClient(plan={"reuse": False, "fork": {"session_key": "sid-new", "records": 4}})

    desktop.maybe_execute(cfg, client, "rid", _turn(id="turn-2"), "th")

    assert "has 2 records, not 4" in client.of("fail_turn")[0][1][1]
    assert len(runs) == 1 and not client.of("record_session")


def test_a_fork_is_not_handed_to_the_app_holding_its_source(cfg, runs, tmp_path, monkeypatch):
    _source_with_transcript(cfg, runs, tmp_path, monkeypatch)
    channel = Path(desktop._index(cfg)["sid-new"]["worktree"]) / desktop.CHANNEL
    (channel / "alive").write_text(str(int(time.time() * 1000)))  # the source is open in the app
    client = FakeClient(plan={"reuse": False, "fork": {"session_key": "sid-new", "records": 4}})

    _go(cfg, client, _turn(id="turn-2", prompt="fork prompt"))

    assert not list(channel.glob("fu-*.txt"))
    assert runs[-1]["resume"] == client.of("record_session")[0][2]["session_id"]


def test_closing_a_fork_keeps_the_worktree_its_source_still_uses(cfg, runs, tmp_path, monkeypatch):
    _source_with_transcript(cfg, runs, tmp_path, monkeypatch)
    fork_sid = desktop.fork_session(cfg, "sid-new", 4, "scratch")
    wt = Path(desktop._index(cfg)["sid-new"]["worktree"])

    assert desktop.close(cfg, fork_sid) == "closed"
    assert wt.exists()
    assert desktop.close(cfg, "sid-new") == "closed"
    assert not wt.exists()


def test_a_headless_turn_is_one_shot_so_background_work_is_refused(monkeypatch):
    """The session ends with the turn: canopy's one_shot_turn_guard must refuse a
    background Agent/Bash there, as on a cloud turn, or its work is silently lost."""
    import canopy_acp

    seen = {}

    def drive_turn(prompt, **kw):
        seen.update(kw["env"])
        return SimpleNamespace(cancelled=False, rate_limit={}, ok=True, text="t", session_id="s")

    monkeypatch.setattr(canopy_acp, "drive_turn", drive_turn)
    headless.run_turn("hi", cwd=Path("/tmp"), emit=lambda e: None, env={"X": "1"})
    assert seen == {"X": "1", headless.HEADLESS_ENV: "1", headless.ONE_SHOT_ENV: "1"}


def test_a_headless_turn_starts_on_its_agents_model(cfg, runs):
    _go(cfg, FakeClient(), _turn(cli_model="opus"))
    assert ENVS[0]["ANTHROPIC_MODEL"] == "opus"
    _go(cfg, FakeClient(), _turn(id="turn-0002-bbbb", cli_model="opus; rm -rf"))
    assert ENVS[1].get("ANTHROPIC_MODEL") != "opus; rm -rf"
