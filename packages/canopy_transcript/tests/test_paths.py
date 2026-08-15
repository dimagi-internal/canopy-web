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

from canopy_transcript import resolve_emdash_transcript


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
