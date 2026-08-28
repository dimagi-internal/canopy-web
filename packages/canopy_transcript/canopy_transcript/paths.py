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
    """Claude Code's ``~/.claude/projects/<name>`` encoding: '/', '.' and '_' -> '-'.

    The underscore was missing until 2026-07-27 and is not cosmetic: a cwd
    containing one resolved to a directory that does not exist, so the session
    silently had no transcript — no durable rows, no backfill, no reset. It bit
    a session run under ``packages/canopy_runtime``.

    Verified against the live fleet rather than guessed: of 286 directories in
    ``~/.claude/projects`` **none** contains an underscore, and the session run
    in ``…/packages/canopy_runtime`` is stored as ``…-packages-canopy-runtime``.

    Keep this in step with ``runner/ec2/cloud_runner.py::_encode_project_dir``,
    which is the same function on the box that ships as a single file.
    """
    return str(worktree).replace("/", "-").replace(".", "-").replace("_", "-")


# emdash 1.2 renamed the worktree ROOT as well as the leaf: a task now lives at
# `worktrees/<repo>-<8 hex>/emdash-<task>-<suffix>` instead of
# `worktrees/<repo>/emdash/<task>-<suffix>`. The 8-hex tail is per-repo, not
# per-task, so it cannot be derived from (repo, task) — it has to be read off disk.
_REPO_DIR_RE = re.compile(r"^(?P<repo>.+)-[0-9a-f]{8}$")
_FLAT_TASK_PREFIX = "emdash-"


def _worktree_bases(repo: str, task: str, home: Path) -> list[Path]:
    """Candidate worktree paths for (repo, task), most-specific first.

    Three layouts are live on the fleet, all of them observed rather than guessed:

    * `<repo>/emdash/<task>` — the common pre-1.2 nesting (verified 2026-07-20).
    * `<repo>/<task>` — some worktrees (e.g. echo's) skip the `emdash` segment.
    * `<repo>-<8 hex>/emdash-<task>` — **emdash 1.2**. Every worktree created after
      the 1.2 upgrade uses it (verified 2026-08-28: all four such dirs on the fleet
      laptop were created minutes after 1.2 first ran, and every older one is the
      pre-1.2 shape).

    The 1.2 root carries a random per-repo hash, so unlike the other two this
    candidate cannot be constructed — the worktree root is globbed off disk. That
    makes the list empty for a repo with no checkout yet, which is correct: a base
    we cannot name is a base we cannot match.

    A wrong guess simply doesn't match a project dir and degrades to no transcript.
    """
    root = home / "emdash" / "worktrees" / repo
    bases = [root / "emdash" / task, root / task]
    try:
        for repo_dir in sorted((home / "emdash" / "worktrees").glob(f"{repo}-*")):
            if not repo_dir.is_dir():
                continue
            hashed = _REPO_DIR_RE.match(repo_dir.name)
            # The glob is a prefix match, so `canopy-*` also lands on
            # `canopy-web-05b9fcc4`. Only the repo half of the split may be
            # trusted: without this, repo `canopy` borrows `canopy-web`'s
            # transcripts — a wrong session's history, which is worse than none.
            if hashed and hashed.group("repo") == repo:
                bases.append(repo_dir / f"{_FLAT_TASK_PREFIX}{task}")
    except OSError:
        pass
    return bases


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

    Project dirs OUTLIVE their worktrees, and a task name gets reused, so the
    de-dupe suffix means one name can accumulate several — four `bednet*` dirs
    existed locally on 2026-08-14, three of them orphans of deleted worktrees.
    Newest-mtime alone would happily return one of those (touch an old session and
    it wins), so a dir whose worktree still exists is preferred; mtime only breaks
    ties among the live ones. Orphans remain the fallback rather than an error —
    an unrecognised layout must degrade to the old behaviour, not to no transcript.
    """
    if not repo or not task or not claude_home.is_dir():
        return None
    live: list[Path] = []
    orphaned: list[Path] = []
    for base in _worktree_bases(repo, task, home):
        prefix = encode_project_dir(base)
        for proj_dir in claude_home.glob(prefix + "*"):
            if not proj_dir.is_dir():
                continue
            rest = proj_dir.name[len(prefix):]
            if rest != "" and not _SUFFIX_RE.fullmatch(rest):
                continue
            # `rest` is the de-dupe suffix verbatim, and the lossy part of the
            # encoding is confined to `base` — which we hold as a real path — so
            # the worktree this dir was encoded from reconstructs exactly.
            worktree = base if rest == "" else base.with_name(base.name + rest)
            bucket = live if worktree.is_dir() else orphaned
            bucket.extend(proj_dir.glob("*.jsonl"))
    candidates = live or orphaned
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


def parse_emdash_worktree(cwd: Path | str, *, home: Path) -> tuple[str, str] | None:
    """The inverse of `_worktree_bases`: a worktree path -> (repo, task), or None.

    A Claude Code hook reports the session's `cwd`, but canopy identifies a
    session by (project, session_key) — so the live path needs to run the
    convention backwards. Handles all three observed layouts
    (`<repo>/emdash/<task>`, `<repo>/<task>`, and emdash 1.2's
    `<repo>-<8 hex>/emdash-<task>`) and strips emdash's random de-dupe suffix,
    so `…/canopy-web/emdash/runner-yipnn` resolves to ("canopy-web", "runner").

    The 1.2 shape has to be recognised explicitly rather than falling through to
    the two-segment case: that case would hand back the hashed dir as the repo
    (`canopy-web-05b9fcc4`) and the prefixed dir as the task
    (`emdash-emdash-check-sq69z`), and NEITHER survives `emdash_task_candidates`
    — so the session matches nothing and the hook silently describes a session
    canopy does not believe exists.

    The suffix strip is ambiguous by construction — a task legitimately named
    `foo-bar` is indistinguishable from task `foo` with suffix `bar`. Both
    candidates are therefore returned to the caller's matcher via
    `emdash_task_candidates`; this returns the *exact* pair only, and None when
    the path is not under the worktree root at all.
    """
    try:
        rel = Path(cwd).resolve().relative_to((home / "emdash" / "worktrees").resolve())
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if len(parts) >= 3 and parts[1] == "emdash":
        return parts[0], parts[2]
    if len(parts) >= 2:
        hashed = _REPO_DIR_RE.match(parts[0])
        if hashed and parts[1].startswith(_FLAT_TASK_PREFIX):
            # emdash 1.2. The de-dupe suffix is left ON the task, exactly as the
            # other layouts leave it — `emdash_task_candidates` is the one place
            # that strips it, and it needs the raw name to offer both readings.
            return hashed.group("repo"), parts[1][len(_FLAT_TASK_PREFIX):]
        return parts[0], parts[1]
    return None


def emdash_task_candidates(task: str) -> list[str]:
    """The task names a worktree dir could correspond to, most-specific first.

    emdash appends a random `-<suffix>` to de-dupe worktree dirs, and a task may
    also legitimately contain a hyphen, so the mapping is genuinely ambiguous:
    `runner-yipnn` is either task `runner-yipnn` or task `runner`. Returning both
    lets the caller match against the sessions it actually knows about instead of
    guessing — the exact name wins when it exists.
    """
    out = [task]
    stripped = _SUFFIX_RE.sub("", task)
    if stripped and stripped != task:
        out.append(stripped)
    return out
