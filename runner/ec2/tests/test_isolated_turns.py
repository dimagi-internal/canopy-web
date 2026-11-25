"""A learning run's turn keeps nothing on the box for the agent's later turns (canopy-web `harness.isolation`).

canopy-web marks such a turn `isolated` and hands it only to a box that reports `isolated` on its heartbeat; the
box turns Claude Code's auto memory off for it, so nothing it read reaches the memory later turns load, and deletes its
Claude session once it ends, so no later turn reads it off the disk."""
from __future__ import annotations


def test_the_heartbeat_says_this_code_isolates(cloud_runner, tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(tmp_path))
    assert cloud_runner._heartbeat_body([])["isolated"] == cloud_runner.ISOLATED_VERSION == 2


def test_an_isolated_turn_runs_without_auto_memory_and_any_other_turn_as_before(cloud_runner):
    assert cloud_runner._isolation_env({"id": "t", "isolated": True}) == {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
    assert cloud_runner._isolation_env({"id": "t", "isolated": False}) == {}
    assert cloud_runner._isolation_env({"id": "t"}) == {}             # an older canopy-web: unchanged


def _session_files(cloud_runner, tmp_path, monkeypatch, cwd, sid):
    monkeypatch.setattr(cloud_runner, "CALLER_ROOT", tmp_path / "caller")
    monkeypatch.setattr(cloud_runner, "CLAUDE_PROJECTS_HOME", tmp_path / "projects")
    project = tmp_path / "projects" / cloud_runner._encode_project_dir(cwd)
    files = [tmp_path / "caller" / "t1.json", project / f"{sid}.jsonl", project / sid / "tool-results" / "r.txt"]
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("what the run read")
    return files


def test_an_isolated_turn_leaves_no_session_on_the_box(cloud_runner, tmp_path, monkeypatch):
    cwd = tmp_path / "agents" / "ada"
    files = _session_files(cloud_runner, tmp_path, monkeypatch, cwd, "sid-1")
    cloud_runner._isolated_forget_turn({"id": "t1", "isolated": True}, cwd, ["sid-1", "", None])
    assert not any(f.exists() for f in files)


def test_any_other_turn_keeps_its_session_to_resume(cloud_runner, tmp_path, monkeypatch):
    cwd = tmp_path / "agents" / "ada"
    files = _session_files(cloud_runner, tmp_path, monkeypatch, cwd, "sid-1")
    cloud_runner._isolated_forget_turn({"id": "t1"}, cwd, ["sid-1"])
    assert all(f.exists() for f in files)
