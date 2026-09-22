"""A chat with an agent on the cloud box IS that agent.

Until 2026-09-22 a cloud chat with hal ran in a bare scratch directory with no
hal repo (no persona, none of hal's own hooks) and none of hal's credentials, and
its canopy-web MCP never authenticated. These pin the three halves of the fix:
the session gets a worktree of the agent's repo, the turn gets the agent's env,
and the env names the agent in a variable Claude Code passes to MCP helpers.
"""
from __future__ import annotations

import subprocess


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _agent_clone(root):
    """A bare 'origin' and a clone of it at AGENT_ROOT/hal, like bootstrap makes."""
    origin = root / "origin-hal"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "CLAUDE.md").write_text("You are Hal.\n")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=origin)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1", cwd=origin)
    agents = root / "agents"
    agents.mkdir()
    _git("clone", "-q", str(origin), str(agents / "hal"), cwd=root)
    return origin, agents


def _chat(sid="sess-1"):
    return {"agent_slug": "hal", "origin_ref": {"chat_session_id": sid}}


def test_a_chat_with_hal_runs_in_a_worktree_of_hals_repo(cloud_runner, monkeypatch, tmp_path):
    origin, agents = _agent_clone(tmp_path)
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(agents))
    monkeypatch.setattr(cloud_runner, "WORK_DIR", str(tmp_path / "work"))
    cwd = cloud_runner._turn_cwd(_chat(), "t1")
    assert (cwd / "CLAUDE.md").read_text() == "You are Hal.\n"
    # Stable, so --resume keeps resolving: the same session maps to the same path.
    assert cloud_runner._turn_cwd(_chat(), "t2") == cwd
    assert "sessions" in cwd.parts


def test_a_clean_session_worktree_follows_the_agents_repo(cloud_runner, monkeypatch, tmp_path):
    origin, agents = _agent_clone(tmp_path)
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(agents))
    monkeypatch.setattr(cloud_runner, "WORK_DIR", str(tmp_path / "work"))
    cloud_runner._turn_cwd(_chat(), "t1")
    (origin / "CLAUDE.md").write_text("You are Hal, v2.\n")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "v2", cwd=origin)
    assert (cloud_runner._turn_cwd(_chat(), "t2") / "CLAUDE.md").read_text() == "You are Hal, v2.\n"


def test_a_session_with_local_work_is_left_alone(cloud_runner, monkeypatch, tmp_path):
    origin, agents = _agent_clone(tmp_path)
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(agents))
    monkeypatch.setattr(cloud_runner, "WORK_DIR", str(tmp_path / "work"))
    cwd = cloud_runner._turn_cwd(_chat(), "t1")
    (cwd / "notes.md").write_text("mid-work")
    (origin / "CLAUDE.md").write_text("v2\n")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "v2", cwd=origin)
    cloud_runner._turn_cwd(_chat(), "t2")
    assert (cwd / "CLAUDE.md").read_text() == "You are Hal.\n"
    assert (cwd / "notes.md").exists()


def test_a_pre_existing_plain_session_dir_is_not_touched(cloud_runner, monkeypatch, tmp_path):
    # A session from before this change: its --resume target lives under exactly
    # this path, so turning it into a worktree would lose the conversation.
    _, agents = _agent_clone(tmp_path)
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(agents))
    monkeypatch.setattr(cloud_runner, "WORK_DIR", str(tmp_path / "work"))
    legacy = tmp_path / "work" / "sessions" / "sess-1"
    legacy.mkdir(parents=True)
    (legacy / "keep").write_text("x")
    assert cloud_runner._turn_cwd(_chat(), "t1") == legacy
    assert not (legacy / "CLAUDE.md").exists() and (legacy / "keep").exists()


def test_a_chat_turn_carries_the_agents_identity(cloud_runner):
    assert cloud_runner._turn_agent_slug(_chat()) == "hal"


def test_the_env_names_the_agent_for_mcp_helpers(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path)
    (tmp_path / ".hal").mkdir()
    (tmp_path / ".hal" / ".env").write_text("CANOPY_WEB_PAT=hal-pat\n")
    env = cloud_runner._agent_env("hal")
    assert env["CANOPY_AGENT"] == "hal" and env["CANOPY_WEB_PAT"] == "hal-pat"
    # Named even before the agent's env is provisioned.
    assert cloud_runner._agent_env("ada")["CANOPY_AGENT"] == "ada"
