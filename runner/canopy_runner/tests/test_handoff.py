"""Packing a session's work on one box and unpacking it on another (handoff.py).

Real git throughout: a bare `origin`, a source clone with work in every state a
session leaves it in, and a target worktree made the way a runner makes one (a
fresh worktree of its own clone at origin/main)."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from canopy_runner import handoff


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(bare), str(seed))
    (seed / "app.py").write_text("print('v1')\n")
    (seed / "README.md").write_text("readme\n")
    (seed / ".gitignore").write_text(".env\nbuild/\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "push", "-q", "origin", "main")
    return bare


def clone(origin, path):
    git(path.parent, "clone", "-q", str(origin), str(path))
    return path


def target_worktree(origin, tmp_path, name="target"):
    """A runner's fresh worktree: its own clone, detached at origin/main."""
    c = clone(origin, tmp_path / f"{name}-clone")
    wt = tmp_path / name
    git(c, "worktree", "add", "-q", "--detach", str(wt), "origin/main")
    return wt


@pytest.fixture
def dirty_source(origin, tmp_path):
    """Every state at once: a never-pushed branch with two local commits, a
    staged change, an unstaged change on top of it, a deleted file, untracked
    files (one binary, one nested), an ignored .env and an unignored secret."""
    src = clone(origin, tmp_path / "source")
    git(src, "checkout", "-q", "-b", "feature/never-pushed")
    (src / "lib.py").write_text("x = 1\n")
    git(src, "add", "lib.py")
    git(src, "commit", "-q", "-m", "local one")
    (src / "lib.py").write_text("x = 2\n")
    git(src, "commit", "-q", "-am", "local two")
    (src / "app.py").write_text("print('staged')\n")
    git(src, "add", "app.py")
    (src / "app.py").write_text("print('staged')\nprint('unstaged on top')\n")
    git(src, "rm", "-q", "README.md")
    (src / "notes.txt").write_text("new, untracked\n")
    (src / "pkg").mkdir()
    (src / "pkg" / "blob.bin").write_bytes(bytes(range(256)) * 4)
    (src / ".env").write_text("SECRET=ignored\n")
    (src / ".env.local").write_text("SECRET=not-ignored-but-secret\n")
    return src


def test_capture_records_every_kind_of_work(dirty_source):
    archive, m = handoff.capture(dirty_source)
    assert m["branch"] == "feature/never-pushed"
    assert m["local_commits"] == 2
    assert m["staged_files"] == ["README.md", "app.py"] or sorted(m["staged_files"]) == ["README.md", "app.py"]
    assert m["unstaged_files"] == ["app.py"]
    assert sorted(m["untracked_files"]) == ["notes.txt", "pkg/blob.bin"]
    # The ignored .env is never even considered; the unignored one is skipped AND named.
    assert [s["path"] for s in m["skipped"]] == [".env.local"]
    assert m["repo"].endswith("origin")
    assert m["bytes"] == len(archive)
    members = handoff._members(archive)
    assert ".env" not in str(list(members)) or all(".env" not in k for k in members)
    assert "local.bundle" in members


def test_round_trip_matches_source_status_and_diff(origin, dirty_source, tmp_path):
    archive, m = handoff.capture(dirty_source)
    wt = target_worktree(origin, tmp_path)
    report = handoff.restore(wt, archive)
    assert report["ok"], report
    assert git(wt, "rev-parse", "HEAD") == git(dirty_source, "rev-parse", "HEAD")
    assert git(wt, "symbolic-ref", "--short", "HEAD") == "feature/never-pushed"
    expected = [e for e in handoff.status_entries(dirty_source) if e != "?? .env.local"]
    assert handoff.status_entries(wt) == expected
    assert git(wt, "diff", "--binary", "HEAD") == git(dirty_source, "diff", "--binary", "HEAD")
    assert git(wt, "diff", "--cached") == git(dirty_source, "diff", "--cached")
    assert (wt / "pkg" / "blob.bin").read_bytes() == (dirty_source / "pkg" / "blob.bin").read_bytes()
    assert not (wt / ".env").exists() and not (wt / ".env.local").exists()
    assert "git status matches" in handoff.summary(report)


def test_pushed_branch_needs_no_bundle(origin, tmp_path):
    src = clone(origin, tmp_path / "source")
    git(src, "checkout", "-q", "-b", "pushed")
    (src / "a.txt").write_text("a\n")
    git(src, "add", "a.txt")
    git(src, "commit", "-q", "-m", "a")
    git(src, "push", "-q", "-u", "origin", "pushed")
    (src / "a.txt").write_text("a changed\n")
    archive, m = handoff.capture(src)
    assert m["local_commits"] == 0 and m["upstream"] == "origin/pushed"
    assert "local.bundle" not in handoff._members(archive)
    wt = target_worktree(origin, tmp_path)
    report = handoff.restore(wt, archive)
    assert report["ok"], report
    assert git(wt, "rev-parse", "--abbrev-ref", "@{u}") == "origin/pushed"


def test_clean_tree_restores_just_the_branch(origin, tmp_path):
    src = clone(origin, tmp_path / "source")
    archive, m = handoff.capture(src)
    assert m["status"] == [] and m["local_commits"] == 0
    report = handoff.restore(target_worktree(origin, tmp_path), archive)
    # `main` is held by the target clone's own checkout, so it lands detached at
    # the same commit — and says so rather than failing.
    assert report["ok"] and report["head"] == m["head"]
    assert any("checked out elsewhere" in n for n in report["notes"])


def test_restore_refuses_a_target_with_tracked_changes(origin, dirty_source, tmp_path):
    archive, _ = handoff.capture(dirty_source)
    wt = target_worktree(origin, tmp_path)
    (wt / "app.py").write_text("someone's work here\n")
    report = handoff.restore(wt, archive)
    assert not report["ok"] and "already has uncommitted changes" in report["error"]
    assert (wt / "app.py").read_text() == "someone's work here\n"


def test_runtime_files_already_in_the_target_are_left_alone(origin, dirty_source, tmp_path):
    """emdash / the desktop runtime write their own settings as they make the
    worktree; those are not work, and must neither block the restore nor fail
    its check."""
    archive, _ = handoff.capture(dirty_source)
    wt = target_worktree(origin, tmp_path)
    (wt / ".claude").mkdir()
    (wt / ".claude" / "settings.local.json").write_text("{}")
    report = handoff.restore(wt, archive)
    assert report["ok"], report
    assert (wt / ".claude" / "settings.local.json").read_text() == "{}"


def test_restore_never_overwrites_an_untracked_file(origin, dirty_source, tmp_path):
    archive, _ = handoff.capture(dirty_source)
    wt = target_worktree(origin, tmp_path)
    (wt / "notes.txt").write_text("mine\n")
    report = handoff.restore(wt, archive)
    assert not report["ok"] and "would be overwritten" in report["error"]
    assert (wt / "notes.txt").read_text() == "mine\n"


def test_restore_refuses_a_different_repo(dirty_source, tmp_path, origin):
    archive, _ = handoff.capture(dirty_source)
    other = tmp_path / "other.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(other))
    wt = target_worktree(origin, tmp_path)
    git(wt, "remote", "set-url", "origin", str(other))
    report = handoff.restore(wt, archive)
    assert not report["ok"] and "the snapshot is of" in report["error"]


def test_size_cap_drops_the_transcript_first_then_refuses(dirty_source, tmp_path):
    transcript = tmp_path / "1234-abcd.jsonl"
    transcript.write_text("".join(json.dumps({"n": i, "pad": os.urandom(150).hex()}) + "\n" for i in range(50)))
    archive, m = handoff.capture(dirty_source, transcript=transcript)
    assert m["transcript"] == {"session_id": "1234-abcd", "records": 50}
    assert handoff.read_transcript(archive) == transcript.read_bytes()
    small = len(handoff.capture(dirty_source)[0])
    _, m2 = handoff.capture(dirty_source, transcript=transcript, max_bytes=small + 512)
    assert m2["transcript"] is None and m2["transcript_dropped"]
    with pytest.raises(handoff.SnapshotError, match="over the"):
        handoff.capture(dirty_source, max_bytes=100)


def test_marker_lives_in_the_git_dir_not_the_tree(origin, tmp_path):
    wt = target_worktree(origin, tmp_path)
    path = handoff.write_marker(wt, {"ok": True})
    assert path is not None and json.loads(path.read_text()) == {"ok": True}
    assert handoff.status_entries(wt) == []


def test_normalize_remote_spellings():
    same = {handoff.normalize_remote(u) for u in (
        "https://github.com/dimagi-internal/canopy-web.git",
        "git@github.com:dimagi-internal/canopy-web.git",
        "https://x-access-token:abc@github.com/dimagi-internal/canopy-web",
    )}
    assert same == {"github.com/dimagi-internal/canopy-web"}


def test_unsafe_paths_are_never_written(origin, tmp_path):
    import io
    import tarfile

    src = clone(origin, tmp_path / "source")
    archive, _ = handoff.capture(src)
    members = handoff._members(archive)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in {**members, "untracked/../escape.txt": b"x",
                           "untracked/.git/hooks/post-checkout": b"x"}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    wt = target_worktree(origin, tmp_path)
    report = handoff.restore(wt, buf.getvalue())
    assert not (tmp_path / "escape.txt").exists()
    assert len([n for n in report["notes"] if "unsafe path" in n]) == 2
