"""ccd_runner without a Claude app: the pure helpers, and one turn's whole loop
against a fake control plane with the mod's half of the file protocol simulated.

The live half (a real app importing the session and the real mod submitting) is
proven by hand against tests/fake_harness.py; see canopy-web#1188.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "ccd_runner", Path(__file__).resolve().parents[1] / "ccd_runner.py")
ccd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ccd)


def test_thread_key_is_explicit_or_per_turn():
    assert ccd.thread_key({"id": "t1", "origin_ref": {"thread_key": "k"}}) == "k"
    assert ccd.thread_key({"id": "t1", "origin_ref": {"thread_id": "j"}}) == "j"
    assert ccd.thread_key({"id": "t1"}) == "turn:t1"


def test_slug_is_branch_safe():
    assert ccd.slug_for({"id": "abcdef1234", "project": "My Repo/x"}) == "my-repo-x-abcdef12"


def test_worktree_settings_merges_allow_and_enables_the_mod():
    out = ccd.worktree_settings(["Bash", "Write"], {"permissions": {"allow": ["Read"]}, "x": 1})
    assert out["permissions"]["allow"] == ["Bash", "Read", "Write"]
    assert out["enabledPlugins"][ccd.PLUGIN] is True
    assert out["x"] == 1


def test_events_from_assistant_and_tool_result_records():
    a = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hi"}, {"type": "tool_use", "id": "u1", "name": "Bash", "input": {"c": 1}}]}}
    u = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "u1", "content": [{"type": "text", "text": "ok"}]}]}}
    assert [e["kind"] for e in ccd.events_from_record(a)] == ["assistant", "tool_start"]
    end = ccd.events_from_record(u)[0]
    assert end["kind"] == "tool_end" and end["payload"]["content"] == "ok"
    assert ccd.events_from_record({"type": "user", "message": {"content": "plain"}}) == []


def test_batches_respect_the_byte_cap():
    out = ccd.batches(["a" * 6, "b" * 6, "c" * 6], limit=13)
    assert out == [["a" * 6, "b" * 6], ["c" * 6]]


def test_read_token_from_env_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("OTHER=1\nexport CANOPY_WEB_PAT='abc'\n")
    assert ccd.read_token(f"@{f}#CANOPY_WEB_PAT") == "abc"
    assert ccd.read_token("raw") == "raw"


class FakeApi:
    def __init__(self, plan=None):
        self.calls = []
        self.plan = plan or {"reuse": False}

    def call(self, method, path, body=None, tries=3):
        self.calls.append((path, body))
        if path.endswith("/resolve-session"):
            return 200, self.plan
        return 200, {}

    def paths(self):
        return [p for p, _ in self.calls]

    def bodies(self, suffix):
        return [b for p, b in self.calls if p.endswith(suffix)]


def _repo(tmp_path) -> Path:
    repo = tmp_path / "scratch"
    repo.mkdir()
    for argv in (["git", "init", "-q", "-b", "main"], ["git", "commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(argv, cwd=repo, check=True, env={
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    return repo


def _fake_mod(channel: Path, which: str, sid: str, answer: str):
    """What the mod does in the app: see the prompt, submit it, finish the turn."""
    target = channel / ("task.txt" if which == "task" else f"{which}.txt")
    for _ in range(200):
        if target.exists() and (which != "task" or (channel / "seeded").exists()):
            break
        time.sleep(0.02)
    t = int(time.time() * 1000)
    for i, (kind, extra) in enumerate([("submitted", {"which": which}),
                                       ("ask", {"tool": "Bash"}),
                                       ("turn.complete", {"answer": answer, "isAborted": False})]):
        (channel / f"ev-{t + i}-{i}-{kind}.json").write_text(
            json.dumps({"t": t + i, "sid": sid, "kind": kind, "extra": extra}))


@pytest.fixture
def no_app(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(ccd, "ensure_app", lambda: True)
    monkeypatch.setattr(ccd, "open_session", opened.append)
    monkeypatch.setattr(ccd, "prepare_settings", lambda cfg, wt: None)
    monkeypatch.setattr(ccd, "seed_session", lambda cfg, wt: "sid-1")
    monkeypatch.setattr(ccd, "CLAUDE_HOME", tmp_path / "claude-projects")
    return opened


def test_a_new_thread_gets_a_worktree_a_session_and_a_finish(tmp_path, no_app):
    repo = _repo(tmp_path)
    cfg = ccd.load_config(_write_cfg(tmp_path, repo))
    turn = {"id": "turn-0001-aaaa", "project": "scratch", "prompt": "do it",
            "origin_ref": {"thread_key": "th"}}
    api = FakeApi()
    wt = Path(cfg["workdir"]) / "worktrees" / "scratch" / ccd.slug_for(turn)
    mod = threading.Thread(target=_fake_mod, args=(wt / ccd.CHANNEL, "task", "sid-1", "all done"))
    mod.start()
    ccd.TurnRun(api, cfg, turn).run()
    mod.join()

    assert (wt / ccd.CHANNEL / "task.txt").read_text() == "do it"
    assert no_app == ["sid-1"]  # imported into the app once
    assert api.bodies("/start") == [{"session_id": "sid-1"}]
    assert api.bodies("/record-session")[0]["session_key"] == "sid-1"
    statuses = [e["payload"]["status"] for b in api.bodies("/events") for e in b["events"]
                if e["kind"] == "status"]
    assert statuses == ["created_session", "submitted", "needs_input"]
    assert api.bodies("/finish") == [{"status": "done", "result_note": "all done", "session_key": "sid-1"}]
    # The runner's own files stay out of the repo's status.
    st = subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True).stdout
    assert ccd.CHANNEL not in st


def test_a_continued_thread_delivers_a_followup_and_wakes_a_stopped_session(tmp_path, no_app):
    repo = _repo(tmp_path)
    cfg = ccd.load_config(_write_cfg(tmp_path, repo))
    wt = tmp_path / "existing-wt"
    (wt / ccd.CHANNEL).mkdir(parents=True)
    (wt / ccd.CHANNEL / "fu-1.txt").write_text("earlier")
    (Path(cfg["workdir"]) / "sessions.json").write_text(json.dumps({"sid-9": str(wt)}))
    api = FakeApi(plan={"reuse": True, "session_key": "sid-9"})
    turn = {"id": "turn-0002-bbbb", "project": "scratch", "prompt": "again",
            "origin_ref": {"thread_key": "th"}}
    mod = threading.Thread(target=_fake_mod, args=(wt / ccd.CHANNEL, "fu-2", "sid-9", "second"))
    mod.start()
    ccd.TurnRun(api, cfg, turn).run()
    mod.join()

    assert (wt / ccd.CHANNEL / "fu-2.txt").read_text() == "again"
    assert no_app == ["sid-9"]  # no `alive` heartbeat -> the session is woken
    assert not api.bodies("/record-session")
    assert api.bodies("/finish")[0]["result_note"] == "second"


def test_an_unknown_project_fails_the_turn_without_touching_the_app(tmp_path, no_app):
    cfg = ccd.load_config(_write_cfg(tmp_path, _repo(tmp_path)))
    api = FakeApi()
    ccd.TurnRun(api, cfg, {"id": "turn-0003-cccc", "project": "nope", "prompt": "x"}).run()
    assert no_app == []
    assert api.bodies("/finish")[0]["status"] == "failed"
    assert "no checkout for 'nope'" in api.bodies("/finish")[0]["result_note"]


def _write_cfg(tmp_path, repo) -> Path:
    p = tmp_path / "runner.json"
    p.write_text(json.dumps({"base_url": "http://x", "token": "t", "runner_id": "r",
                             "projects": {"scratch": str(repo)}, "workdir": str(tmp_path / "work")}))
    (tmp_path / "work").mkdir(exist_ok=True)
    return p
