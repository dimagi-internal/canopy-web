"""A new chat session runs the agent's LATEST published skills and rails, confined or not.

Trellis, 2026-11: skill and CLAUDE.md edits merged to its repo's main never reached freshly
started confined chats until a non-session agent turn happened to pull the clone. A session's
worktree is cut from the clone's origin/main; a caller's turn holds no GitHub token, so its
best-effort fetch of a private repo failed in silence. The runner now refreshes the agent's
clone itself (with the agent's own delegation, in that one git process, never in the turn's
env) and says so, loudly, when it cannot.
"""
from __future__ import annotations

import subprocess

import pytest


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo, msg):
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg, cwd=repo)


@pytest.fixture
def box(cloud_runner, monkeypatch, tmp_path):
    origin = tmp_path / "origin-trellis"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "SKILL.md").write_text("v1\n")
    _commit(origin, "v1")
    _git("config", "receive.denyCurrentBranch", "ignore", cwd=origin)
    agents = tmp_path / "agents"
    agents.mkdir()
    _git("clone", "-q", str(origin), str(agents / "trellis"), cwd=tmp_path)
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(agents))
    monkeypatch.setattr(cloud_runner, "WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(cloud_runner, "_CLONE_FETCH", {})
    monkeypatch.setattr(cloud_runner, "_HEALTH", {})
    return cloud_runner, origin, agents / "trellis", tmp_path


CHAT = {"agent_slug": "trellis", "origin_ref": {"chat_session_id": "sess-1"}}
CONFINED_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}   # an allowlist: no GH_TOKEN


def test_a_new_confined_session_is_cut_from_the_latest_published_main(box):
    mod, origin, clone, _ = box
    (origin / "SKILL.md").write_text("v2 (merged after the box last pulled)\n")
    _commit(origin, "v2")
    cwd = mod._turn_cwd(CHAT, "t1", env=dict(CONFINED_ENV))
    assert (cwd / "SKILL.md").read_text().startswith("v2")
    assert (clone / "SKILL.md").read_text().startswith("v2")   # the clone itself moved too


def test_an_old_clean_session_is_brought_forward_on_its_next_turn(box):
    mod, origin, _, _ = box
    cwd = mod._turn_cwd(CHAT, "t1", env=dict(CONFINED_ENV))
    (origin / "SKILL.md").write_text("v3\n")
    _commit(origin, "v3")
    assert mod._turn_cwd(CHAT, "t2", env=dict(CONFINED_ENV)) == cwd
    assert (cwd / "SKILL.md").read_text() == "v3\n"


def test_the_refresh_uses_the_agents_delegation_but_never_hands_it_to_the_turn(box, monkeypatch):
    mod, _, _, _ = box
    monkeypatch.setattr(mod, "SENSITIVE_DATA_MODE", False)
    monkeypatch.setattr(mod, "_delegated_value",
                        lambda slug, name: "ghp_agent" if (slug, name) == ("trellis", "github-token") else "")
    turn_env = dict(CONFINED_ENV)
    fetch_env = mod._clone_refresh_env("trellis", turn_env)
    assert fetch_env["GH_TOKEN"] == "ghp_agent" and fetch_env["PATH"] == CONFINED_ENV["PATH"]
    assert "GH_TOKEN" not in turn_env and "GITHUB_TOKEN" not in turn_env   # the turn's own env is untouched


def test_the_refresh_borrows_nothing_when_the_turn_has_its_own_or_the_box_is_sensitive(box, monkeypatch):
    mod, _, _, _ = box
    monkeypatch.setattr(mod, "_delegated_value", lambda slug, name: "ghp_agent")
    own = {"GH_TOKEN": "turn-token"}
    assert mod._clone_refresh_env("trellis", own) is own
    monkeypatch.setattr(mod, "SENSITIVE_DATA_MODE", True)
    env = dict(CONFINED_ENV)
    assert mod._clone_refresh_env("trellis", env) is env
    monkeypatch.setattr(mod, "SENSITIVE_DATA_MODE", False)
    monkeypatch.setattr(mod, "_delegated_value", lambda slug, name: "")
    assert mod._clone_refresh_env("trellis", env) is env      # no delegation: fetch as before


def test_a_fetch_that_fails_is_logged_and_turns_the_clones_check_amber(box, monkeypatch):
    mod, origin, clone, tmp = box
    logs: list[str] = []
    monkeypatch.setattr(mod, "_log", logs.append)
    _git("remote", "set-url", "origin", str(tmp / "gone"), cwd=clone)   # a repo it cannot reach
    assert mod._fetch_clone(clone, dict(CONFINED_ENV)) is False
    assert any("could not update" in line and "trellis" in line for line in logs)
    now = mod._CLONE_FETCH[str(clone)]["failed_at"]
    mod._clones_check(now)                       # just cloned: not stale yet
    assert mod._HEALTH["clones"]["status"] == "ok"
    mod._clones_check(now + mod.CLONE_STALE_SECONDS + 3600)
    check = mod._HEALTH["clones"]
    assert check["status"] == "warn" and "trellis" in check["detail"] and "STALE" in check["detail"]


def test_the_check_clears_when_a_fetch_succeeds_again(box, monkeypatch):
    mod, _, clone, tmp = box
    monkeypatch.setattr(mod, "_log", lambda *_: None)
    good = _git("remote", "get-url", "origin", cwd=clone)
    _git("remote", "set-url", "origin", str(tmp / "gone"), cwd=clone)
    mod._fetch_clone(clone, dict(CONFINED_ENV))
    _git("remote", "set-url", "origin", good, cwd=clone)
    assert mod._fetch_clone(clone, dict(CONFINED_ENV)) is True
    mod._clones_check(mod._CLONE_FETCH[str(clone)]["ok_at"] + 10 * mod.CLONE_STALE_SECONDS)
    assert mod._HEALTH["clones"]["status"] == "ok"
