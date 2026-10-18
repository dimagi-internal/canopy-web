"""Forking a chat on the cloud runner: the fork runs in its source's directory,
and its first turn starts from a cut of the source's transcript.

canopy-web's half (which turn to cut at, `resolve-session` → `fork` and
`worktree_of`) is in tests/test_session_fork.py at the repo root.
"""
from __future__ import annotations

import json
import pathlib

import pytest


def _transcript(cloud_runner, cwd: pathlib.Path, sid: str, n: int) -> pathlib.Path:
    path = cloud_runner.CLAUDE_PROJECTS_HOME / cloud_runner._encode_project_dir(cwd) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"sessionId": sid, "n": i}) + "\n" for i in range(n))
                    + "\nnot json\n")
    return path


@pytest.fixture
def home(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "CLAUDE_PROJECTS_HOME", tmp_path / "projects")
    monkeypatch.setattr(cloud_runner, "WORK_DIR", str(tmp_path / "work"))
    return tmp_path


def test_the_cut_is_a_prefix_of_the_source_under_a_new_session(cloud_runner, home):
    cwd = home / "work" / "sessions" / "src-chat"
    src = _transcript(cloud_runner, cwd, "src-sid", 6)

    sid = cloud_runner._cut_fork(cwd, {"session_key": "src-sid", "records": 4}, "")

    assert sid != "src-sid"
    cut = [json.loads(line) for line in (src.parent / f"{sid}.jsonl").read_text().splitlines()]
    assert [r["n"] for r in cut] == [0, 1, 2, 3]
    assert {r["sessionId"] for r in cut} == {sid}
    assert cloud_runner._resume_target_exists(cwd, sid)
    assert len(src.read_text().splitlines()) == 8  # the source is untouched


def test_a_cut_that_cannot_be_made_says_why(cloud_runner, home):
    cwd = home / "work" / "sessions" / "src-chat"
    _transcript(cloud_runner, cwd, "src-sid", 2)

    with pytest.raises(cloud_runner.ForkError, match="has 2 records, not 4"):
        cloud_runner._cut_fork(cwd, {"session_key": "src-sid", "records": 4}, "")
    with pytest.raises(cloud_runner.ForkError, match="no transcript on this box"):
        cloud_runner._cut_fork(cwd, {"session_key": "gone", "records": 1}, "")


def test_resolve_session_tells_the_turn_where_to_run_and_what_to_cut(cloud_runner, monkeypatch):
    plan = {"reuse": False, "session_key": "", "worktree_of": "src-chat",
            "fork": {"session_key": "src-sid", "records": 4}}
    monkeypatch.setattr(cloud_runner, "_api", lambda method, path, body=None: (200, plan))
    turn = {"id": "t1", "project": "canopy-web", "workspace_slug": "w1",
            "origin_ref": {"chat_session_id": "fork-chat", "thread_key": "fork-chat"}}

    assert cloud_runner._session_resume_plan("rid", turn) == ""
    assert turn["_worktree_of"] == "src-chat"
    assert turn["_fork"] == {"session_key": "src-sid", "records": 4}


def test_a_fork_runs_in_its_sources_directory(cloud_runner, home, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_project_worktree", lambda project, path, env: None)
    turn = {"id": "t1", "origin_ref": {"chat_session_id": "fork-chat"}}

    assert cloud_runner._turn_cwd(turn, "t1").name == "fork-chat"
    turn["_worktree_of"] = "src-chat"
    assert cloud_runner._turn_cwd(turn, "t1") == home / "work" / "sessions" / "src-chat"


def test_a_forks_transcript_streams_from_its_sources_directory(cloud_runner, home):
    path = _transcript(cloud_runner, home / "work" / "sessions" / "src-chat", "fork-sid", 1)

    assert cloud_runner._session_transcript_path("fork-chat", "fork-sid") == path
