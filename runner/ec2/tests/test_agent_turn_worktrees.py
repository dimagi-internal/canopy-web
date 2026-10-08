"""Each cloud AGENT turn runs in its own worktree of the agent's clone (#1141).

Every agent turn used to run IN AGENT_ROOT/<slug>, so two concurrent turns for
one agent shared a working tree — the stranded commits and moving branches #1131
fixed for other repos. These pin the fix and the three things it had to keep
working: the clone's ignored state (.env, node_modules) still reaches the turn, a
chat reply can still resume an agent turn's session, and the stream tail still
finds its transcript — now by looking up where the turn ran, not deriving it.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

import pytest


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo, msg):
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg, cwd=repo)


@pytest.fixture
def box(cloud_runner, monkeypatch, tmp_path):
    """An 'origin', a bootstrapped clone at AGENT_ROOT/hal with ignored state in
    it, and the runner pointed at all of it."""
    origin = tmp_path / "origin-hal"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "CLAUDE.md").write_text("You are Hal.\n")
    (origin / ".gitignore").write_text(".env\nnode_modules/\n")
    (origin / "mcp").mkdir()
    (origin / "mcp" / "server.ts").write_text("//\n")
    _commit(origin, "v1")
    # Let a test push to it (it is checked out on main).
    _git("config", "receive.denyCurrentBranch", "ignore", cwd=origin)
    agents = tmp_path / "agents"
    agents.mkdir()
    clone = agents / "hal"
    _git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    (clone / ".env").write_text("CANOPY_WEB_PAT=x\n")
    (clone / "node_modules" / "tsx").mkdir(parents=True)
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(agents))
    monkeypatch.setattr(cloud_runner, "WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(cloud_runner, "CLAUDE_PROJECTS_HOME", tmp_path / "projects")
    return cloud_runner, origin, clone, tmp_path


def _turn(slug="hal"):
    return {"agent_slug": slug, "origin_ref": {}}


def _transcript(projects, cloud_runner, cwd, sid):
    f = projects / cloud_runner._encode_project_dir(cwd) / f"{sid}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('{"type": "user", "message": {"content": "hi"}}\n')
    return f


# ── a worktree per turn ──────────────────────────────────────────────────────

def test_concurrent_turns_for_one_agent_get_separate_worktrees(box):
    mod, _, clone, tmp = box
    a = mod._turn_cwd(_turn(), "aaaaaaaa-1")
    b = mod._turn_cwd(_turn(), "bbbbbbbb-2")
    assert a != b and a != clone and b != clone
    assert a.parent == tmp / "work" / "agents" / "hal"
    assert (a / "CLAUDE.md").read_text() == (b / "CLAUDE.md").read_text() == "You are Hal.\n"
    (a / "CLAUDE.md").write_text("edited in a\n")
    assert (b / "CLAUDE.md").read_text() == "You are Hal.\n"
    assert (clone / "CLAUDE.md").read_text() == "You are Hal.\n"
    assert _git("status", "--porcelain", cwd=clone) == ""


def test_a_turn_sees_the_agents_latest_commit(box):
    mod, origin, _, _ = box
    (origin / "CLAUDE.md").write_text("You are Hal, v2.\n")
    _commit(origin, "v2")
    assert (mod._turn_cwd(_turn(), "aaaaaaaa-1") / "CLAUDE.md").read_text() == "You are Hal, v2.\n"


def test_the_clones_ignored_state_is_linked_in_and_stays_ignored(box):
    mod, _, clone, _ = box
    wt = mod._turn_cwd(_turn(), "aaaaaaaa-1")
    assert (wt / ".env").read_text() == "CANOPY_WEB_PAT=x\n"
    assert (wt / "node_modules").is_symlink() and (wt / "node_modules" / "tsx").is_dir()
    assert os.path.realpath(wt / ".env") == os.path.realpath(clone / ".env")
    # A symlinked dir does not match `node_modules/`: without the exclude it
    # would be untracked, one `git add -A` from a commit.
    assert _git("status", "--porcelain", cwd=wt) == ""


def test_an_agent_with_no_clone_keeps_the_scratch_dir(box):
    mod, _, _, tmp = box
    assert mod._turn_cwd(_turn("ada"), "aaaaaaaa-1") == tmp / "work" / "aaaaaaaa"


def test_no_worktree_falls_back_to_the_shared_clone(box, monkeypatch):
    mod, _, clone, _ = box
    monkeypatch.setattr(mod, "_ensure_session_worktree",
                        lambda c, p, env=None: p.mkdir(parents=True, exist_ok=True))
    assert mod._turn_cwd(_turn(), "aaaaaaaa-1") == clone


def test_a_chat_session_still_gets_its_stable_session_worktree(box):
    mod, _, _, tmp = box
    chat = {"agent_slug": "hal", "origin_ref": {"chat_session_id": "sess-1"}}
    assert mod._turn_cwd(chat, "t1") == tmp / "work" / "sessions" / "sess-1"


# ── released when the turn is done ───────────────────────────────────────────

def test_a_clean_worktree_is_removed(box):
    mod, _, clone, _ = box
    wt = mod._turn_cwd(_turn(), "aaaaaaaa-1")
    assert mod._release_agent_worktree(wt) is True
    assert not wt.exists()
    assert (clone / ".env").exists() and (clone / "node_modules" / "tsx").is_dir()
    assert str(wt) not in _git("worktree", "list", cwd=clone)


def test_uncommitted_work_is_kept(box):
    mod, _, _, _ = box
    wt = mod._turn_cwd(_turn(), "aaaaaaaa-1")
    (wt / "notes.md").write_text("mid-work")
    assert mod._release_agent_worktree(wt) is False and (wt / "notes.md").exists()


def test_an_unpushed_branch_is_kept_and_a_pushed_one_released(box):
    mod, _, _, _ = box
    wt = mod._turn_cwd(_turn(), "aaaaaaaa-1")
    _git("checkout", "-qb", "fix", cwd=wt)
    (wt / "CLAUDE.md").write_text("fixed\n")
    _commit(wt, "fix")
    assert mod._release_agent_worktree(wt) is False and wt.exists()
    _git("push", "-q", "origin", "fix", cwd=wt)
    _git("fetch", "-q", "origin", cwd=wt)
    assert mod._release_agent_worktree(wt) is True and not wt.exists()


def test_only_agent_turn_worktrees_are_ever_released(box):
    mod, _, clone, tmp = box
    chat = {"agent_slug": "hal", "origin_ref": {"chat_session_id": "sess-1"}}
    session = mod._turn_cwd(chat, "t1")
    assert mod._release_agent_worktree(session) is False and session.exists()
    assert mod._release_agent_worktree(clone) is False and clone.exists()


def test_a_stale_leftover_is_swept_and_a_recent_one_is_not(box):
    mod, _, _, _ = box
    old = mod._turn_cwd(_turn(), "aaaaaaaa-1")
    recent = mod._turn_cwd(_turn(), "bbbbbbbb-2")
    past = time.time() - mod.AGENT_WORKTREE_MAX_AGE - 60
    os.utime(old, (past, past))
    mod._turn_cwd(_turn(), "cccccccc-3")
    assert not old.exists() and recent.exists()


# ── finding the turn's transcript afterwards ─────────────────────────────────

def test_a_recorded_turn_is_found_after_its_worktree_is_gone(box):
    mod, _, _, tmp = box
    wt = mod._turn_cwd(_turn(), "aaaaaaaa-1")
    mod._record_session_cwd("cli-1", wt)
    f = _transcript(tmp / "projects", mod, wt, "cli-1")
    mod._release_agent_worktree(wt)
    assert mod._agent_session_transcript("hal", "cli-1") == f


def test_without_a_record_the_agents_turn_dirs_are_searched(box):
    mod, _, _, tmp = box
    wt = tmp / "work" / "agents" / "hal" / "aaaaaaaa"
    f = _transcript(tmp / "projects", mod, wt, "cli-2")
    assert mod._agent_session_transcript("hal", "cli-2") == f
    assert mod._agent_session_transcript("ada", "cli-2") is None


def test_a_session_from_before_this_is_still_found_in_the_clone(box):
    mod, _, clone, tmp = box
    f = _transcript(tmp / "projects", mod, clone, "cli-3")
    assert mod._agent_session_transcript("hal", "cli-3") == f


def test_only_agent_turn_cwds_are_recorded(box):
    mod, _, clone, tmp = box
    mod._record_session_cwd("cli-x", clone)
    mod._record_session_cwd("cli-y", tmp / "work" / "sessions" / "s")
    assert not (tmp / "work" / "agents" / ".session-cwds.json").exists()


def test_the_record_is_bounded(box, monkeypatch):
    mod, _, _, tmp = box
    monkeypatch.setattr(mod, "_SESSION_CWDS_KEEP", 3)
    wt = tmp / "work" / "agents" / "hal" / "aaaaaaaa"
    for i in range(5):
        mod._record_session_cwd(f"cli-{i}", wt)
    import json
    data = json.loads((tmp / "work" / "agents" / ".session-cwds.json").read_text())
    assert list(data) == ["cli-2", "cli-3", "cli-4"]


def test_a_reply_adopts_the_session_from_the_turns_worktree(box):
    mod, _, _, tmp = box
    wt = mod._turn_cwd(_turn(), "aaaaaaaa-1")
    mod._record_session_cwd("cli-1", wt)
    _transcript(tmp / "projects", mod, wt, "cli-1")
    mod._release_agent_worktree(wt)
    chat_dir = tmp / "work" / "sessions" / "sess-1"
    assert mod._adopt_resume_transcript(chat_dir, "cli-1", "hal") is True
    assert mod._resume_target_exists(chat_dir, "cli-1") is True


def test_the_stream_tail_finds_an_agent_turns_transcript(box):
    mod, _, _, tmp = box
    if mod._transcript_core() is None:
        pytest.skip("canopy_transcript not importable here")
    wt = mod._turn_cwd(_turn(), "aaaaaaaa-1")
    mod._record_session_cwd("cli-1", wt)
    f = _transcript(tmp / "projects", mod, wt, "cli-1")
    assert mod._session_transcript_path("sess-1", "cli-1", "hal") == f


# ── the turn worker wires it together ────────────────────────────────────────

def test_a_turn_records_where_it_ran_and_releases_its_worktree(box, monkeypatch):
    mod, _, _, tmp = box
    monkeypatch.setattr(mod, "_start_lease_renewal", lambda rid, tid: threading.Event())
    monkeypatch.setattr(mod, "_child_safe_env", lambda: {"PATH": os.environ["PATH"]})
    monkeypatch.setattr(mod, "_ship_transcript_rows", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_github_turn_env", lambda rid, turn: {})
    monkeypatch.setattr(mod, "_agent_env", lambda slug: None)
    monkeypatch.setattr(mod, "_api", lambda m, path, body=None, **k: (200, {}))
    ran_in = {}

    def fake_execute(prompt, turn_id, emit, cwd=None, agent_slug=None, resume_session_id=None):
        ran_in["cwd"] = cwd
        mod._announce_session(turn_id, "cli-1")
        _transcript(tmp / "projects", mod, cwd, "cli-1")
        return True, "ok", "cli-1"

    monkeypatch.setattr(mod, "execute_prompt", fake_execute)
    mod._run_turn("r-1", {"id": "aaaaaaaa-0000", "agent_slug": "hal", "prompt": "hi",
                          "origin_ref": {}})
    assert ran_in["cwd"].parent == tmp / "work" / "agents" / "hal"
    assert not ran_in["cwd"].exists(), "a clean turn worktree is released after the finish"
    assert mod._agent_session_transcript("hal", "cli-1") is not None
