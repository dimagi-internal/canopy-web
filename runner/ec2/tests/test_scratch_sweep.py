"""A cloud runner clears the scratch its turns leave behind, on its own.

cloud-ec2-2's root reached 100% on 2026-10-19 15:22 — 1Password could not write
its session file under /tmp, so ada's vault refresh failed and turn 86535ffc
crashed. The caches bootstrap prunes were not the cause: /tmp held ~3.4 GB of
week-old scratch clones agents made themselves (/tmp/tmp.*, /tmp/ada-*,
/tmp/sweep, ...), and WORK_DIR/sessions held 37 chat worktrees (one 1.1 GB);
nothing removed either, and the only remedy the box offered was a human refresh.

So the runner sweeps, when idle, every few hours and whenever the disk check
reads warn: old scratch in /tmp, old chat-session worktrees, then the caches if
the disk is still full. It NEVER deletes work that exists nowhere else — a git
clone with uncommitted changes or commits on no remote is kept, and named in a
`scratch` health check (two such clones were on that box).
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import time
from collections import namedtuple

import pytest

DAY = 86400.0
OLD = time.time() - 30 * DAY


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo, msg):
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg, cwd=repo)


def _age(path: pathlib.Path, when: float = OLD) -> None:
    """Make everything under `path` look untouched since `when`."""
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            os.utime(os.path.join(root, name), (when, when), follow_symlinks=False)
    os.utime(path, (when, when), follow_symlinks=False)


@pytest.fixture
def box(cloud_runner, monkeypatch, tmp_path):
    """A /tmp, a WORK_DIR, an origin repo and an agent clone, all under tmp_path."""
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    work = tmp_path / "work"
    (work / "sessions").mkdir(parents=True)
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "README").write_text("x\n")
    _commit(origin, "v1")
    _git("config", "receive.denyCurrentBranch", "ignore", cwd=origin)
    agents = tmp_path / "agents"
    agents.mkdir()
    _git("clone", "-q", str(origin), str(agents / "hal"), cwd=tmp_path)
    monkeypatch.setattr(cloud_runner, "SCRATCH_ROOT", str(scratch))
    monkeypatch.setattr(cloud_runner, "WORK_DIR", str(work))
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(agents))
    monkeypatch.setattr(cloud_runner, "CLAUDE_PROJECTS_HOME", tmp_path / "projects")
    # Its own in-flight set: a sweep stops at the first turn it sees, and under
    # xdist a turn another test on this worker left running (or never popped) would
    # stop it before the first candidate — every "kept" assertion then reads [].
    monkeypatch.setattr(cloud_runner, "_IN_FLIGHT", {})
    return cloud_runner, scratch, work, origin, agents


def _clone(origin, dest):
    subprocess.run(["git", "clone", "-q", str(origin), str(dest)], check=True, capture_output=True)
    return dest


def _health(cr):
    return {c["name"]: c for c in cr.health_report()["checks"]}


def _seen(cr, path: pathlib.Path) -> str:
    """What a sweep would see at `path`, taken BEFORE it runs (its own `git status`
    rewrites files under .git). For the failure message: the clone-with-commits case
    has failed intermittently in CI only (canopy-web#1756's merge queue, main push
    39628295690) with an empty `kept` and nothing logged, which leaves three silent
    skips — not a candidate, touched recently, or no unsaved work found."""
    now = time.time()
    newest = max(((os.lstat(os.path.join(r, n)).st_mtime, os.path.join(r, n))
                  for r, ds, fs in os.walk(path) for n in ds + fs), default=(0, ""))
    return (f"candidate={any(c == path for c, _ in cr._scratch_candidates(now))} "
            f"touched={cr._touched_since(path, now - cr.SCRATCH_MAX_AGE_SECONDS)} "
            f"newest={newest[1]} {now - newest[0]:.0f}s ago")


# ── /tmp scratch ────────────────────────────────────────────────────────────────

def test_old_scratch_is_removed(box):
    cr, scratch, *_ = box
    junk = scratch / "tmp.JUjoVAZa7U"
    (junk / "deep").mkdir(parents=True)
    (junk / "deep" / "big.bin").write_bytes(b"x" * 4096)
    note = scratch / "ada-dry.json"
    note.write_text("{}")
    _age(junk)
    _age(note)
    report = cr.sweep_scratch()
    assert not junk.exists() and not note.exists()
    assert report["freed"] >= 4096


def test_scratch_touched_recently_anywhere_inside_is_kept(box):
    """A directory's own mtime only moves when an entry is added or removed; a
    clone being worked in changes files deep inside it. The newest mtime anywhere
    in the tree is what says it is in use."""
    cr, scratch, *_ = box
    live = scratch / "canopy-fix"
    (live / "src").mkdir(parents=True)
    (live / "src" / "a.py").write_text("x")
    _age(live)
    os.utime(live / "src" / "a.py")  # edited just now
    cr.sweep_scratch()
    assert live.exists()


def test_a_clone_with_commits_on_no_remote_is_kept_and_named(box):
    cr, scratch, _work, origin, _ = box
    clone = _clone(origin, scratch / "ada-cw-inv7")
    _git("checkout", "-qb", "ada/inv7-needrestart", cwd=clone)
    (clone / "fix.sh").write_text("x")
    _commit(clone, "fix(runner/ec2): keep needrestart from restarting the runner mid-turn")
    _age(clone)
    seen = _seen(cr, clone)
    report = cr.sweep_scratch()
    assert clone.exists()
    assert [str(clone)] == [k["path"] for k in report["kept"]], (
        f"{seen} unsaved={cr._unsaved_git_work(clone)!r}")
    check = _health(cr)["scratch"]
    assert check["status"] == "warn"
    assert str(clone) in check["detail"] and "needrestart" in check["detail"]


def test_a_branch_not_checked_out_still_counts(box):
    """`git log --branches --not --remotes`: an unpushed branch the clone has
    switched away from is as unrecoverable as the checked-out one."""
    cr, scratch, _work, origin, _ = box
    clone = _clone(origin, scratch / "work")
    _git("checkout", "-qb", "feat/membership-audit", cwd=clone)
    (clone / "f").write_text("x")
    _commit(clone, "feat(workspaces): audit membership changes")
    _git("checkout", "-q", "main", cwd=clone)
    _age(clone)
    cr.sweep_scratch()
    assert clone.exists()


def test_a_clone_nested_in_a_scratch_dir_protects_the_whole_dir(box):
    cr, scratch, _work, origin, _ = box
    parent = scratch / "ada-43e84a56"
    parent.mkdir()
    _clone(origin, parent / "canopy-web")
    dirty = _clone(origin, parent / "canopy")
    (dirty / "README").write_text("changed\n")
    _age(parent)
    cr.sweep_scratch()
    assert dirty.exists()


def test_a_clean_pushed_clone_is_removed(box):
    cr, scratch, _work, origin, _ = box
    clone = _clone(origin, scratch / "sweep")
    _age(clone)
    cr.sweep_scratch()
    assert not clone.exists()


def test_the_runners_own_dirs_and_system_entries_are_never_touched(box, monkeypatch):
    cr, scratch, *_ = box
    work = scratch / "canopy-runner-work"
    work.mkdir()
    monkeypatch.setattr(cr, "WORK_DIR", str(work))
    hidden = scratch / ".X11-unix"
    private = scratch / "systemd-private-abc"
    for d in (hidden, private):
        d.mkdir()
    for d in (work, hidden, private):
        _age(d)
    cr.sweep_scratch()
    assert work.exists() and hidden.exists() and private.exists()


def test_another_users_scratch_is_left_alone(box, monkeypatch):
    cr, scratch, *_ = box
    theirs = scratch / "aws"
    theirs.mkdir()
    _age(theirs)
    real_lstat = os.lstat

    def lstat(path, *a, **kw):
        st = real_lstat(path, *a, **kw)
        if pathlib.Path(path) == theirs:
            fields = list(st)
            fields[4] = st.st_uid + 1  # st_uid
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(cr.os, "lstat", lstat)
    cr.sweep_scratch()
    assert theirs.exists()


def test_nothing_is_swept_while_a_turn_runs(box):
    cr, scratch, *_ = box
    junk = scratch / "tmp.old"
    junk.mkdir()
    _age(junk)
    cr._IN_FLIGHT["turn-1"] = None
    try:
        cr.sweep_scratch()
    finally:
        cr._IN_FLIGHT.pop("turn-1", None)
    assert junk.exists()


def test_a_sweep_interrupted_mid_delete_is_finished_next_time(box):
    """The sweep renames before it deletes, so a turn can never be handed a
    half-deleted directory; a crash between the two leaves a tombstone, which
    the next sweep removes whatever its age."""
    cr, scratch, *_ = box
    tomb = scratch / ".canopy-reaped-tmp.x-123"
    tomb.mkdir()
    (tomb / "f").write_text("x")
    cr.sweep_scratch()
    assert not tomb.exists()


# ── chat-session worktrees ──────────────────────────────────────────────────────

def _session(cr, sid="06986abd-497e-4460-8eca-5fe782617cfc"):
    turn = {"agent_slug": "hal", "origin_ref": {"chat_session_id": sid}}
    return cr._turn_cwd(turn, "turn-abc-123"), turn


def test_an_idle_clean_session_worktree_is_removed_and_comes_back_on_its_next_turn(box):
    cr, _scratch, work, *_ = box
    path, turn = _session(cr)
    assert (path / ".git").exists()
    _age(path)
    cr.sweep_scratch()
    assert not path.exists()
    # The conversation is not lost: its transcript lives under ~/.claude/projects,
    # keyed by this same path, and the next turn re-makes the worktree there.
    again = cr._turn_cwd(turn, "turn-abc-456")
    assert again == path and (path / ".git").exists()


def test_a_session_whose_transcript_moved_recently_is_kept(box):
    """A long chat's worktree can sit unchanged for weeks while the conversation
    goes on; its transcript is what moves every turn."""
    cr, _scratch, work, *_ = box
    path, _turn = _session(cr)
    _age(path)
    tdir = cr.CLAUDE_PROJECTS_HOME / cr._encode_project_dir(path)
    tdir.mkdir(parents=True)
    (tdir / "s.jsonl").write_text("{}\n")
    cr.sweep_scratch()
    assert path.exists()


def test_a_session_with_uncommitted_work_is_kept(box):
    cr, _scratch, work, *_ = box
    path, _turn = _session(cr)
    (path / "notes.md").write_text("half done\n")
    _age(path)
    report = cr.sweep_scratch()
    assert path.exists()
    assert str(path) in [k["path"] for k in report["kept"]]


def test_a_session_worktree_does_not_inherit_the_clones_other_branches(box):
    """A worktree shares refs with its clone. Judging it by `--branches` would
    keep every session forever once any branch of the agent's clone is unpushed."""
    cr, _scratch, work, _origin, agents = box
    clone = agents / "hal"
    _git("checkout", "-qb", "local-only", cwd=clone)
    (clone / "x").write_text("x")
    _commit(clone, "only here")
    _git("checkout", "-q", "main", cwd=clone)
    path, _turn = _session(cr)
    _age(path)
    cr.sweep_scratch()
    assert not path.exists()


# ── when it runs ────────────────────────────────────────────────────────────────

_Usage = namedtuple("_Usage", "total used free")


@pytest.fixture
def hygiene(cloud_runner, monkeypatch):
    """maybe_sweep_disk with the sweep and the cache prune recorded, run inline."""
    cr = cloud_runner
    monkeypatch.setenv("DISK_SWEEP", "1")
    calls = []
    monkeypatch.setattr(cr, "sweep_scratch", lambda: calls.append("sweep") or {"freed": 0, "kept": []})
    monkeypatch.setattr(cr, "_prune_caches", lambda: calls.append("prune"))
    monkeypatch.setattr(cr, "_start_hygiene", lambda target: target())
    pct = {"v": 50}
    monkeypatch.setattr(cr.shutil, "disk_usage",
                        lambda p: _Usage(100 * 2**30, pct["v"] * 2**30, (100 - pct["v"]) * 2**30))
    return cr, calls, pct


def test_an_idle_box_sweeps_every_few_hours(hygiene):
    cr, calls, _ = hygiene
    assert cr.maybe_sweep_disk(now=1000.0) is True
    assert calls == ["sweep"]
    assert cr.maybe_sweep_disk(now=1000.0 + 60) is False
    assert cr.maybe_sweep_disk(now=1000.0 + cr.SWEEP_INTERVAL_SECONDS) is True


def test_a_full_disk_sweeps_sooner_and_prunes_the_caches_too(hygiene):
    cr, calls, pct = hygiene
    cr.maybe_sweep_disk(now=1000.0)
    calls.clear()
    pct["v"] = 85
    assert cr.maybe_sweep_disk(now=1000.0 + cr.SWEEP_PRESSURE_SECONDS) is True
    assert calls == ["sweep", "prune"]


def test_a_box_with_room_does_not_prune_the_caches(hygiene):
    cr, calls, _ = hygiene
    cr.maybe_sweep_disk(now=1000.0)
    assert "prune" not in calls


def test_off_switch(hygiene, monkeypatch):
    cr, calls, _ = hygiene
    monkeypatch.setenv("DISK_SWEEP", "0")
    assert cr.maybe_sweep_disk(now=1000.0) is False and calls == []


def test_never_while_a_turn_runs(hygiene):
    cr, calls, pct = hygiene
    pct["v"] = 95
    cr._IN_FLIGHT["t"] = None
    try:
        assert cr.maybe_sweep_disk(now=1000.0) is False
    finally:
        cr._IN_FLIGHT.pop("t", None)
    assert calls == []


def test_the_disk_check_no_longer_asks_a_person_to_refresh(cloud_runner, monkeypatch):
    cr = cloud_runner
    monkeypatch.setattr(cr.shutil, "disk_usage", lambda p: _Usage(100, 85, 15))
    by = _health(cr)
    assert by["disk"]["status"] == "warn"
    assert "refresh this runner" not in by["disk"]["detail"]
