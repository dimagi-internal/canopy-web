"""Resolve a Claude Code transcript file, by either convention.

Claude Code stores every session as JSONL under
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl``, where the directory name
is the working directory with ``/`` and ``.`` replaced by ``-``. Both runners
need to find that file; they differ only in what they know about the session:

- **emdash-driven** (laptop): knows a repo and a task name, not a session id, so
  it derives the worktree path by convention and takes the newest transcript in
  the matching project dir.
- **`claude -p`** (cloud): knows the cwd it launched in and the CLI's own session
  id, so it addresses the file directly.

Both were implemented separately before this module existed, and had already
converged on the *same* encoding function by accident — which is the clearest
sign they belonged together.

Nothing here raises or guesses: an unresolvable transcript returns ``None``, so
a caller degrades to "no transcript yet" rather than reading the wrong session's
history.
"""
from __future__ import annotations

import re
from pathlib import Path

# emdash appends a short random suffix to a worktree dir name to de-dupe it
# (e.g. the task "ace-nutrition-demo-9619-0720-1352" lives in a worktree dir
# "...-1352-cysov"). The Claude transcript project dir therefore ends in the
# task name OR the task name + "-<suffix>". This matches that trailing suffix.
_SUFFIX_RE = re.compile(r"-[0-9a-z]+$")


def encode_project_dir(worktree: Path | str) -> str:
    """Claude Code's ``~/.claude/projects/<name>`` encoding: '/' and '.' -> '-'."""
    return str(worktree).replace("/", "-").replace(".", "-")


def _worktree_bases(repo: str, task: str, home: Path) -> list[Path]:
    """Candidate worktree paths for (repo, task). Verified against the live fleet
    (2026-07-20): most agents nest under `<repo>/emdash/<task>`, but some
    worktrees sit directly at `<repo>/<task>` with no `emdash` segment. Try both;
    a wrong guess simply doesn't match a project dir and degrades to no
    transcript."""
    root = home / "emdash" / "worktrees" / repo
    return [root / "emdash" / task, root / task]


def resolve_emdash_transcript(
    repo: str, task: str, *, home: Path, claude_home: Path
) -> Path | None:
    """Newest transcript .jsonl for an emdash (repo, task), or None.

    Tolerant of two real-world facts the naive path missed: worktree dirs carry a
    random de-dupe suffix, and the layout isn't uniform (see `_worktree_bases`).
    For each candidate base we glob the encoded prefix and accept a project dir
    that is the exact encoding OR the encoding plus a `-<suffix>` tail, then take
    the newest .jsonl across all matches. A wrong guess returns None, never a
    wrong transcript from an unrelated task — the prefix is anchored at the
    parent segment, so `mobile` can't match `alt-mobile`.
    """
    if not repo or not task or not claude_home.is_dir():
        return None
    candidates: list[Path] = []
    for base in _worktree_bases(repo, task, home):
        prefix = encode_project_dir(base)
        for proj_dir in claude_home.glob(prefix + "*"):
            if not proj_dir.is_dir():
                continue
            rest = proj_dir.name[len(prefix):]
            if rest == "" or _SUFFIX_RE.fullmatch(rest):
                candidates.extend(proj_dir.glob("*.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def resolve_cli_transcript(
    cwd: Path | str, session_id: str, *, claude_home: Path
) -> Path | None:
    """The transcript `claude -p` writes for (cwd, session_id), or None.

    Exact rather than newest-wins: the cloud runner captures the CLI's own
    session id from the `system`/`init` event, so it can address the file
    directly instead of guessing. This is the same lookup `--resume` validation
    already performs — a missing file means "not resumable" there and "no
    transcript yet" here, and both callers must treat it as a fact rather than
    retrying into a wrong guess.
    """
    if not session_id:
        return None
    try:
        path = claude_home / encode_project_dir(cwd) / f"{session_id}.jsonl"
        return path if path.is_file() else None
    except OSError:
        return None
