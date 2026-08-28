"""Transcript path encoding.

`encode_project_dir` is how BOTH runners find a session's transcript on disk.
Getting it wrong does not raise — it resolves to a directory that does not
exist, so the session silently has no durable record at all.
"""
from canopy_transcript import encode_project_dir




def test_underscores_encode_to_dashes():
    """Claude Code maps '_' to '-' like '/' and '.'. Missing it meant a cwd with
    an underscore resolved to a directory that does not exist — the session
    silently had no transcript, so no durable rows and nothing to reset from.

    Verified against the live fleet: of 286 dirs in ~/.claude/projects none
    contains an underscore, and a session run in `…/packages/canopy_runtime` is
    stored as `…-packages-canopy-runtime`.
    """
    assert encode_project_dir("/Users/j/repo/packages/canopy_runtime") == (
        "-Users-j-repo-packages-canopy-runtime")


def test_encoding_collapses_every_separator_it_knows():
    assert encode_project_dir("/a/b.c/d_e") == "-a-b-c-d-e"


# ---------------------------------------------------------------------------
# resolve_emdash_transcript — a project dir outlives its worktree (issue #615)
# ---------------------------------------------------------------------------

import os

from canopy_transcript import (
    emdash_task_candidates,
    parse_emdash_worktree,
    resolve_emdash_transcript,
)


def _plant(home, claude_home, repo, worktree_name, stem, *, mtime, make_worktree):
    """A project dir (+ transcript) for `<repo>/emdash/<worktree_name>`, with the
    worktree itself present or already deleted."""
    worktree = home / "emdash" / "worktrees" / repo / "emdash" / worktree_name
    if make_worktree:
        worktree.mkdir(parents=True)
    proj = claude_home / encode_project_dir(worktree)
    proj.mkdir(parents=True)
    jsonl = proj / f"{stem}.jsonl"
    jsonl.write_text("{}\n")
    os.utime(jsonl, (mtime, mtime))
    return jsonl


def test_a_live_worktree_beats_a_newer_orphan(tmp_path):
    """The de-dupe suffix means one reused task name accumulates project dirs —
    four `bednet*` existed locally on 2026-08-14, three of them orphans of deleted
    worktrees. Newest-mtime alone will happily return an orphan the moment anything
    touches it, which is a silently wrong conversation, not an error."""
    home, claude_home = tmp_path / "home", tmp_path / "home" / ".claude" / "projects"
    _plant(home, claude_home, "ace", "bednet-6u2w6", "live", mtime=1000, make_worktree=True)
    _plant(home, claude_home, "ace", "bednet-mcuto", "orphan", mtime=9000, make_worktree=False)

    got = resolve_emdash_transcript("ace", "bednet", home=home, claude_home=claude_home)
    assert got is not None and got.stem == "live"


def test_mtime_still_decides_among_live_worktrees(tmp_path):
    home, claude_home = tmp_path / "home", tmp_path / "home" / ".claude" / "projects"
    _plant(home, claude_home, "ace", "bednet-aaaaa", "older", mtime=1000, make_worktree=True)
    _plant(home, claude_home, "ace", "bednet-bbbbb", "newer", mtime=9000, make_worktree=True)

    got = resolve_emdash_transcript("ace", "bednet", home=home, claude_home=claude_home)
    assert got is not None and got.stem == "newer"


def test_an_orphan_is_still_better_than_nothing(tmp_path):
    """Degrade to the old behaviour, never to "no transcript" — an unrecognised
    layout must not cost a session its durable record."""
    home, claude_home = tmp_path / "home", tmp_path / "home" / ".claude" / "projects"
    _plant(home, claude_home, "ace", "bednet-mcuto", "orphan", mtime=1000, make_worktree=False)

    got = resolve_emdash_transcript("ace", "bednet", home=home, claude_home=claude_home)
    assert got is not None and got.stem == "orphan"


def test_a_sibling_task_is_never_borrowed(tmp_path):
    """`bednet` must not match `bednet-2` — the suffix pattern is one `-<token>`,
    and a second segment means a different task."""
    home, claude_home = tmp_path / "home", tmp_path / "home" / ".claude" / "projects"
    _plant(home, claude_home, "ace", "bednet-2-tsnn3", "sibling", mtime=9000, make_worktree=True)

    assert resolve_emdash_transcript("ace", "bednet", home=home, claude_home=claude_home) is None


# --------------------------------------------------------------------------------------
# emdash 1.2's worktree layout. Both the repo root and the leaf changed shape:
# `worktrees/<repo>-<8 hex>/emdash-<task>-<suffix>` replaced
# `worktrees/<repo>/emdash/<task>-<suffix>`. Verified on the fleet laptop 2026-08-28 —
# every worktree created after 1.2 first ran uses it, every older one does not.
# --------------------------------------------------------------------------------------

def _plant_v12(home, claude_home, repo, repo_hash, worktree_name, stem, *, make_worktree=True):
    """A project dir (+ transcript) for emdash 1.2's `<repo>-<hash>/emdash-<name>`."""
    worktree = home / "emdash" / "worktrees" / f"{repo}-{repo_hash}" / f"emdash-{worktree_name}"
    if make_worktree:
        worktree.mkdir(parents=True)
    proj = claude_home / encode_project_dir(worktree)
    proj.mkdir(parents=True)
    jsonl = proj / f"{stem}.jsonl"
    jsonl.write_text("{}\n")
    return jsonl


def test_the_1_2_layout_resolves(tmp_path):
    """Before this, every session created on emdash 1.2 resolved to no transcript at
    all — so it streamed nothing and backfilled nothing, silently, forever."""
    home, claude_home = tmp_path / "home", tmp_path / "home" / ".claude" / "projects"
    _plant_v12(home, claude_home, "canopy-web", "05b9fcc4", "emdash-check-sq69z", "live")

    got = resolve_emdash_transcript("canopy-web", "emdash-check", home=home, claude_home=claude_home)
    assert got is not None and got.stem == "live"


def test_the_1_2_layout_does_not_borrow_a_sibling_repo(tmp_path):
    """`canopy` must not match `canopy-web-05b9fcc4` — the hash is 8 hex characters,
    so `web-05b9fcc4` is not one and the dir belongs to a different repo."""
    home, claude_home = tmp_path / "home", tmp_path / "home" / ".claude" / "projects"
    _plant_v12(home, claude_home, "canopy-web", "05b9fcc4", "emdash-check-sq69z", "other")

    assert resolve_emdash_transcript("canopy", "emdash-check", home=home, claude_home=claude_home) is None


def test_the_pre_1_2_layouts_still_resolve(tmp_path):
    """The three layouts coexist: a fleet mid-upgrade has live worktrees in all of
    them, so recognising 1.2 must not cost the older two."""
    home, claude_home = tmp_path / "home", tmp_path / "home" / ".claude" / "projects"
    _plant(home, claude_home, "ace", "spark-ry12q", "nested", mtime=1000, make_worktree=True)

    got = resolve_emdash_transcript("ace", "spark", home=home, claude_home=claude_home)
    assert got is not None and got.stem == "nested"


# --- the inverse, used by the hook path -------------------------------------------------

def test_parse_reads_the_1_2_layout_back(tmp_path):
    """A hook reports a cwd; canopy has to turn it back into (repo, task). Falling
    through to the plain two-segment case yields ("canopy-web-05b9fcc4",
    "emdash-emdash-check-sq69z") — a repo and a task canopy has never heard of, so the
    hook describes a session nothing matches."""
    home = tmp_path / "home"
    cwd = home / "emdash" / "worktrees" / "canopy-web-05b9fcc4" / "emdash-emdash-check-sq69z"
    cwd.mkdir(parents=True)

    repo, task = parse_emdash_worktree(cwd, home=home)
    assert repo == "canopy-web"
    assert "emdash-check" in emdash_task_candidates(task)


def test_parse_leaves_the_older_layouts_alone(tmp_path):
    home = tmp_path / "home"
    for parts, want in [
        (("ace", "emdash", "spark-ry12q"), ("ace", "spark")),
        (("echo", "thread-abc12"), ("echo", "thread")),
    ]:
        cwd = home.joinpath("emdash", "worktrees", *parts)
        cwd.mkdir(parents=True)
        repo, task = parse_emdash_worktree(cwd, home=home)
        assert repo == want[0]
        assert want[1] in emdash_task_candidates(task)
