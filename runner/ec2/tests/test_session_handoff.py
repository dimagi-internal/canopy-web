"""A transfer onto or off a cloud box carries the work (canopy_runner/handoff.py).

The packing itself is tested in runner/canopy_runner/tests/test_handoff.py; this
is the cloud box's use of it: packing a chat session's WORK_DIR/sessions/<id>
worktree as a source, restoring into it as a target (before Claude starts), and
putting the source's transcript where `--resume` finds it. canopy-web's half is
tests/test_session_handoff.py at the repo root.
"""
from __future__ import annotations

import base64
import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "canopy_runner"))
from canopy_runner import handoff  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def box(cloud_runner, monkeypatch, tmp_path):
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@e.com"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@e.com")):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cloud_runner, "CLAUDE_PROJECTS_HOME", tmp_path / "projects")
    monkeypatch.setattr(cloud_runner, "WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(cloud_runner, "_handoff_core", lambda: handoff)
    calls: list = []

    def api(method, path, body=None, **kw):
        calls.append((method, path, body))
        if path.endswith("/handoffs"):
            return 200, {"handoffs": box.due}
        return 200, {}

    monkeypatch.setattr(cloud_runner, "_api", api)
    box.calls, box.due, box.tmp = calls, [], tmp_path
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / "app.py").write_text("v1\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "push", "-q", "origin", "main")
    box.origin = origin
    return box


def _worktree(box, path: pathlib.Path) -> pathlib.Path:
    """A session directory the way `_turn_cwd` makes one: a detached worktree."""
    clone = box.tmp / f"clone-{path.name}"
    if not clone.exists():
        git(box.tmp, "clone", "-q", str(box.origin), str(clone))
    path.parent.mkdir(parents=True, exist_ok=True)
    git(clone, "worktree", "add", "-q", "--detach", str(path), "origin/main")
    return path


def _dirty(wt: pathlib.Path) -> None:
    git(wt, "checkout", "-q", "-b", "wip")
    (wt / "app.py").write_text("v2\n")
    git(wt, "commit", "-q", "-am", "local only")
    (wt / "app.py").write_text("v3 uncommitted\n")
    (wt / "new.txt").write_text("untracked\n")


def test_a_laptop_snapshot_restores_into_the_session_dir_and_resumes(cloud_runner, box):
    laptop = _worktree(box, box.tmp / "laptop" / "task")
    _dirty(laptop)
    sid = "0b5e3c9a-1111-4222-8333-444455556666"
    transcript = box.tmp / f"{sid}.jsonl"
    transcript.write_text(json.dumps({"sessionId": sid, "type": "user"}) + "\n")
    archive, manifest = handoff.capture(laptop, transcript=transcript)
    box_cwd = _worktree(box, cloud_runner._session_workdir("chat-1"))
    cloud_runner._api_bytes = lambda path: archive

    resumed = cloud_runner._restore_handoff("r1", {"id": "h1", "resume": True}, box_cwd, None)

    assert resumed == sid
    assert cloud_runner._resume_target_exists(box_cwd, sid)
    assert handoff.status_entries(box_cwd) == handoff.status_entries(laptop)
    assert git(box_cwd, "rev-parse", "HEAD") == git(laptop, "rev-parse", "HEAD")
    marker = json.loads(handoff.marker_path(box_cwd).read_text())
    assert marker["ok"] is True
    (method, path, body), = [c for c in box.calls if c[1].endswith("/restored")]
    assert path == "/runners/r1/handoffs/h1/restored" and body["report"]["ok"] is True


def test_no_resume_unless_canopy_planned_it(cloud_runner, box):
    laptop = _worktree(box, box.tmp / "laptop" / "task")
    transcript = box.tmp / "abcdef12-0000-0000-0000-000000000000.jsonl"
    transcript.write_text("{}\n")
    archive, _ = handoff.capture(laptop, transcript=transcript)
    box_cwd = _worktree(box, cloud_runner._session_workdir("chat-2"))
    cloud_runner._api_bytes = lambda path: archive
    assert cloud_runner._restore_handoff("r1", {"id": "h2", "resume": False}, box_cwd, None) == ""


def test_a_missing_snapshot_is_reported_not_swallowed(cloud_runner, box):
    box_cwd = _worktree(box, cloud_runner._session_workdir("chat-3"))
    cloud_runner._api_bytes = lambda path: None
    assert cloud_runner._restore_handoff("r1", {"id": "h3", "resume": True}, box_cwd, None) == ""
    marker = json.loads(handoff.marker_path(box_cwd).read_text())
    assert marker["ok"] is False and "could not download" in marker["error"]


def test_as_the_source_it_packs_the_session_dir_and_uploads_it_in_parts(cloud_runner, box):
    wt = _worktree(box, cloud_runner._session_workdir("chat-src"))
    _dirty(wt)
    box.due = [{"id": "h4", "session_id": "chat-src", "session_key": "sid", "project": "",
                "capture": True, "close": False, "transcript": False}]

    cloud_runner._drain_handoffs("r1")

    parts = [b for m, p, b in box.calls if p.endswith("/archive")]
    archive = b"".join(base64.b64decode(b["data"]) for b in parts)
    (_, _, captured), = [c for c in box.calls if c[1].endswith("/captured")]
    assert captured["ok"] is True and captured["manifest"]["sha256"]
    target = _worktree(box, box.tmp / "elsewhere" / "wt")
    assert handoff.restore(target, archive)["ok"]


def test_as_the_source_a_session_with_no_worktree_says_so(cloud_runner, box):
    box.due = [{"id": "h5", "session_id": "never-here", "session_key": "sid",
                "capture": True, "close": False}]
    cloud_runner._drain_handoffs("r1")
    (_, _, captured), = [c for c in box.calls if c[1].endswith("/captured")]
    assert captured["ok"] is False and "no worktree" in captured["error"]


def test_closing_sets_the_session_dir_aside_so_a_transfer_back_starts_clean(cloud_runner, box):
    wt = _worktree(box, cloud_runner._session_workdir("chat-6"))
    _dirty(wt)
    box.due = [{"id": "h6", "session_id": "chat-6", "session_key": "sid",
                "capture": False, "close": True}]

    cloud_runner._drain_handoffs("r1")

    assert not wt.exists()
    aside = [p for p in wt.parent.iterdir() if p.name.startswith("chat-6.handed-off-")]
    assert len(aside) == 1 and (aside[0] / "new.txt").exists()
    (_, _, result), = [c for c in box.calls if c[1].endswith("/close-result")]
    assert result == {"ok": True}
