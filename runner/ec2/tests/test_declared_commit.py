"""A turn in a capability its author declared PHI-safe runs the DECLARED commit
(`agent_sha` on the claim), never main; and a ZDR box runs only confined turns.

docs/superpowers/specs/2026-10-22-phi-agents-on-zdr-runners-design.md §5.
"""
from __future__ import annotations

import subprocess

import pytest

CAP = {"name": "data", "entry": None, "tools": ["Read"], "bash": [], "read_paths": ["{cwd}/**"],
       "data": "phi"}
TID = "3f2b8c1e-0000-4000-8000-0000000000aa"


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture()
def repo(cloud_runner, tmp_path, monkeypatch):
    """An agent clone under AGENT_ROOT with two commits, and a turn worktree at HEAD."""
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(tmp_path / "agents"))
    clone = tmp_path / "agents" / "labsdata"
    clone.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=clone)
    who = ("-c", "user.email=t@t", "-c", "user.name=t")
    for i in (1, 2):
        (clone / "f.txt").write_text(str(i))
        _git("add", "f.txt", cwd=clone)
        _git(*who, "commit", "-qm", f"c{i}", cwd=clone)
    first = _git("rev-list", "--max-parents=0", "HEAD", cwd=clone)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "--detach", str(wt), "HEAD", cwd=clone)
    return cloud_runner, clone, wt, first, _git("rev-parse", "HEAD", cwd=clone)


def test_the_worktree_is_moved_to_the_declared_commit(repo):
    cr, clone, wt, first, head = repo
    assert _git("rev-parse", "HEAD", cwd=wt) == head
    cr._checkout_declared(wt, "labsdata", first)
    assert _git("rev-parse", "HEAD", cwd=wt) == first
    assert _git("rev-parse", "HEAD", cwd=clone) == head  # the shared clone never moves


@pytest.mark.parametrize("sha", ["main", "", "f" * 40])
def test_a_commit_that_cannot_be_checked_out_stops_the_turn(repo, sha):
    cr, _clone, wt, _first, _head = repo
    with pytest.raises(cr.ConfineError):
        cr._checkout_declared(wt, "labsdata", sha)


def test_the_shared_clone_is_never_where_a_declared_turn_runs(repo):
    cr, clone, _wt, first, _head = repo
    with pytest.raises(cr.ConfineError, match="of its own"):
        cr._checkout_declared(clone, "labsdata", first)


# --- in _run_turn --------------------------------------------------------------------


def _turn(**kw):
    d = {"id": TID, "agent_slug": "labsdata", "prompt": "how many visits?", "mcp_token": "cct_x",
         "origin_ref": {}, "caller_context": {"profile": "confined", "capability": CAP,
                                              "conversation": {}}}
    d.update(kw)
    return d


def _wire(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(mod, "CALLER_ROOT", tmp_path / "caller")
    monkeypatch.setattr(mod, "_turn_cwd", lambda turn, tid, env=None: tmp_path)
    monkeypatch.setattr(mod, "_start_lease_renewal", lambda rid, tid: __import__("threading").Event())
    monkeypatch.setattr(mod, "_child_safe_env", lambda: {"PATH": "/bin"})
    calls = {"api": []}
    monkeypatch.setattr(mod, "_api", lambda m, p, b=None, **k: calls["api"].append((m, p, b)) or (200, {}))
    monkeypatch.setattr(mod, "_record_session_resume", lambda *a: None)
    monkeypatch.setattr(mod, "_ship_transcript_rows", lambda *a: None)

    def fake_execute(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        calls["exec"] = prompt
        return True, "ok", "cli-1"

    monkeypatch.setattr(mod, "execute_prompt", fake_execute)
    return calls


def _finish(calls):
    return [b for _m, p, b in calls["api"] if p.endswith("/finish")]


def test_a_declared_turn_runs_only_after_its_commit_is_checked_out(cloud_runner, monkeypatch, tmp_path):
    calls = _wire(cloud_runner, monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(cloud_runner, "_checkout_declared",
                        lambda cwd, slug, sha, env=None: seen.append((slug, sha)))
    cloud_runner._run_turn("r-1", _turn(agent_sha="a" * 40))
    assert seen == [("labsdata", "a" * 40)] and "exec" in calls


def test_a_declared_turn_whose_commit_cannot_be_had_is_not_run(cloud_runner, monkeypatch, tmp_path):
    calls = _wire(cloud_runner, monkeypatch, tmp_path)

    def refuse(*a, **k):
        raise cloud_runner.ConfineError("could not check out the declared commit aaaa")

    monkeypatch.setattr(cloud_runner, "_checkout_declared", refuse)
    cloud_runner._run_turn("r-1", _turn(agent_sha="a" * 40))
    assert "exec" not in calls
    [f] = _finish(calls)
    assert f["status"] == "failed" and "declared commit" in f["result_note"]


def test_a_zdr_box_refuses_an_unconfined_turn(load_cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setenv("RUNNER_NAME", "cloud-zdr-1")
    monkeypatch.setenv("RUNNER_DATA_MODE", "zdr")
    mod = load_cloud_runner()
    calls = _wire(mod, monkeypatch, tmp_path)
    mod._run_turn("r-1", _turn(caller_context={"profile": "full", "capability": None}))
    assert "exec" not in calls
    [f] = _finish(calls)
    assert f["status"] == "failed" and f["result_note"].startswith("zdr_unconfined")


def test_the_heartbeat_says_it_checks_out_declared_commits(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "health_report", lambda: {})
    assert cloud_runner._heartbeat_body([])["pins"] == 1
