"""Read the recent message tail of a live emdash session's Claude transcript.

Phase B of the emdash session controller (docs/superpowers/specs/
2026-07-16-emdash-session-controller-design.md). STDLIB ONLY — the runner is
Django-free and cannot import apps.session_sharing.parser; this is the runner's
own minimal tail reader (user/assistant text for the last ~8 messages), not the
full ParsedTurn model the server uses.

emdash stores no conversation content in emdash4.db (the `messages` table is
empty); the content lives in Claude Code's transcript .jsonl under
~/.claude/projects/<encoded-worktree>/<session>.jsonl. There is no session id or
path in the DB, so the transcript is resolved by CONVENTION:

    worktree  = ~/emdash/worktrees/<repo>/emdash/<task>[-<suffix>]   (see below)
    proj dir  = ~/.claude/projects/<worktree with '/' and '.' -> '-'>
    file      = newest *.jsonl in that dir

The convention has two real-world wrinkles the naive path missed (both verified
against the live fleet, 2026-07-20): emdash appends a short random de-dupe suffix
to the worktree dir name (`-cysov`), and the layout is not uniform — some
worktrees sit at `<repo>/<task>` with no `emdash` segment. `resolve_transcript`
handles both by prefix-globbing each candidate base and accepting a `-<suffix>`
tail; the prefix stays anchored at the parent segment so one task can't grab
another's transcript.

A wrong path is a silent wrong-answer, so resolution returns None rather than
guess. Nothing here raises: a missing dir, unreadable file, or malformed line
degrades to an empty tail with a reason string, so the poll tick survives.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path


def _parse_utc(value) -> datetime.datetime | None:
    """A timestamp from either clock -> aware UTC, or None if it isn't one.

    Two formats meet here: Claude Code's record `timestamp` (ISO-8601 with a "Z")
    and emdash's sqlite `last_interacted_at` ("YYYY-MM-DD HH:MM:SS", naive but
    stored in UTC — verified against the live DB). Anything unparseable is None
    rather than a guess.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _is_later(candidate: str, current) -> bool:
    """True when `candidate` is strictly newer than `current`. An unparseable or
    missing `current` loses — a value we can't read is worse than one we can."""
    new = _parse_utc(candidate)
    if new is None:
        return False
    old = _parse_utc(current)
    return old is None or new > old


logger = logging.getLogger(__name__)

MAX_MSG_CHARS = 2000
# Only the last few messages are ever shown, so read at most this many bytes from
# the END of the transcript rather than the whole file — a long session's .jsonl can
# be tens of MB, and the runner reads several of them on every poll tick (top-K).
TAIL_BYTES = 256 * 1024

# emdash appends a short random suffix to a worktree dir name to de-dupe it
# (e.g. the task "ace-nutrition-demo-9619-0720-1352" lives in a worktree dir
# "...-1352-cysov"). The Claude transcript project dir therefore ends in the
# task name OR the task name + "-<suffix>". This matches that trailing suffix.
_SUFFIX_RE = re.compile(r"-[0-9a-z]+$")


# Path resolution moved to canopy_transcript (the cloud runner had independently
# written the same encoding). Re-exported so existing callers and tests keep
# importing it from here.
from canopy_transcript import encode_project_dir  # noqa: E402,F401
from canopy_transcript import resolve_emdash_transcript as _resolve_emdash


def _worktree_bases(repo: str, task: str, home: Path) -> list[Path]:
    """Candidate worktree paths for (repo, task). Verified against the live fleet
    (2026-07-20): most agents nest under `<repo>/emdash/<task>`, but some worktrees
    (e.g. echo's) sit directly at `<repo>/<task>` with no `emdash` segment. Try both;
    a wrong guess simply doesn't match a project dir and degrades to no transcript."""
    root = home / "emdash" / "worktrees" / repo
    return [root / "emdash" / task, root / task]


def resolve_transcript(repo: str, task: str, *, home: Path, claude_home: Path) -> Path | None:
    """Newest transcript .jsonl for (repo, task), or None.

    Thin wrapper over `canopy_transcript.resolve_emdash_transcript`; the
    convention and its two real-world wrinkles are documented there.
    """
    return _resolve_emdash(repo, task, home=home, claude_home=claude_home)


def _assistant_text(content) -> str:
    """The assistant's spoken output for a turn — TEXT blocks only. tool_use blocks
    are intermediate machinery, not final output, so they're dropped."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return " ".join(p for p in parts if p).strip()


def _user_text(content) -> str:
    """A genuine human prompt — string content or text blocks. Returns "" for a turn
    that carries a tool_result: that's an intermediate tool output the harness fed
    back, not something a person typed."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return ""
    parts = [
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return " ".join(p for p in parts if p).strip()


# User turns the HARNESS injects (not typed by a person): task notifications,
# system reminders, the local-command caveat, command stdout, skill bodies.
# Skipped so the tail shows only the real you<->agent conversation.
#
# The rule lives in canopy_transcript, NOT here. This module used to carry its
# own list; the server carried a second one, and they drifted — the server grew
# two prefixes on 2026-07-26 and this copy didn't, so a skill body kept
# rendering in the human's own bubble on the tail path (found 2026-07-30).
# Re-exported under the old private names so existing callers and tests are
# unaffected.
from canopy_transcript import SYSTEM_NOISE_PREFIXES as _SYSTEM_NOISE_PREFIXES  # noqa: E402,F401
from canopy_transcript import is_system_noise as _is_system_noise  # noqa: E402


def read_recent_messages(path: Path, limit: int = 8) -> list[dict]:
    """Last `limit` CONVERSATIONAL messages as [{"role", "text"}], oldest->newest:
    the AI's text replies and genuine human prompts ONLY. Tool calls, tool results,
    subagent (sidechain) turns, and harness-injected system messages are all dropped
    — this is the clean transcript a person expects, not the raw event log.

    Never raises: an unreadable file yields []; a malformed line is skipped. Reads
    only the last TAIL_BYTES of the file (the tail is all we need); if that cuts the
    first line mid-JSON, json.loads skips it — harmless, we only want complete
    recent messages.
    """
    try:
        with path.open("rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    msgs: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("isSidechain"):
            continue
        kind = payload.get("type")
        message = payload.get("message")
        content = message.get("content", "") if isinstance(message, dict) else ""
        if kind == "assistant":
            text = _assistant_text(content)
            if text:
                msgs.append({"role": "assistant", "text": text[:MAX_MSG_CHARS]})
        elif kind == "user":
            text = _user_text(content)
            if text and not _is_system_noise(text):
                msgs.append({"role": "user", "text": text[:MAX_MSG_CHARS]})
    return msgs[-limit:]


def newest_record_time(path: Path) -> str | None:
    """ISO-8601 UTC of the newest record IN the transcript, or None.

    The session's real activity clock. Deliberately NOT the file's mtime: a
    live-but-idle `claude` process rewrites its own transcript from time to time
    without appending a single record, so mtime advances while the conversation is
    dormant (observed on the fleet 2026-07-25: files whose mtime ran 23h and 44h
    ahead of their newest record). What the transcript SAYS happened is the only
    signal that can't drift that way.

    Counts every record type, not just conversational ones: a tool call or a
    subagent turn mid-run is the session working, and a run that is mid-tool-loop
    is exactly when "is this alive?" is being asked. Reads only the last
    TAIL_BYTES like the tail reader; a partial first line just fails to parse.
    Never raises — an unreadable/timestamp-less file yields None, meaning "no
    opinion", and the caller keeps whatever it had.
    """
    try:
        with path.open("rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read()
    except OSError:
        return None
    newest: datetime.datetime | None = None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        ts = _parse_utc(payload.get("timestamp"))
        if ts and (newest is None or ts > newest):
            newest = ts
    return newest.isoformat() if newest else None


def session_tail(
    repo: str,
    task: str,
    *,
    limit: int = 8,
    home: Path | None = None,
    claude_home: Path | None = None,
) -> tuple[list[dict], str]:
    """(messages, reason). reason == "" on success. NEVER raises — see module docstring."""
    home = home or Path.home()
    claude_home = claude_home or (home / ".claude" / "projects")
    try:
        path = resolve_transcript(repo, task, home=home, claude_home=claude_home)
    except Exception:  # noqa: BLE001 — a fragile-half failure must not crash the tick
        logger.debug("transcript resolve failed for %s/%s", repo, task, exc_info=True)
        return [], "resolve-error"
    if path is None:
        return [], "no-transcript"
    msgs = read_recent_messages(path, limit=limit)
    if not msgs:
        return [], "empty-transcript"
    return msgs, ""


def attach_recent_tail(
    sessions: list[dict],
    *,
    count: int = 8,
    limit: int = 8,
    home: Path | None = None,
    claude_home: Path | None = None,
) -> None:
    """Fill recent_messages on the first `count` sessions (the most-recently-active,
    since emdash.list_open_sessions returns newest-first) — the ones the phone shows
    at the top of the list, so each has a glanceable tail without a round trip. In
    place, best-effort, never raises; the bounded tail read keeps K reads/tick cheap.

    `count` caps how many transcripts are read per tick; `limit` caps messages per
    session. Sessions past `count`, or with no resolvable transcript, carry [].

    Also advances `last_interacted_at` to the newest record in the transcript when
    that is later: emdash's own last_interacted_at only tracks emdash's UI, NOT the
    Claude Code session running in the worktree (which the runner drives), so an
    actively-running session looked stale ("45m ago" while mid-turn).

    The two clocks measure different halves of one session, so we take the LATER of
    them rather than letting either win outright. It is deliberately a max and not
    an override: emdash can legitimately be ahead (you touched the task in its UI
    before the CC session wrote anything), and a transcript with no parseable
    timestamps has no opinion at all.
    """
    home = home or Path.home()
    claude_home = claude_home or (home / ".claude" / "projects")
    for s in sessions[:count]:
        try:
            path = resolve_transcript(
                s.get("project", ""), s.get("emdash_task", ""),
                home=home, claude_home=claude_home,
            )
        except Exception:  # noqa: BLE001 — a fragile-half failure must not crash the tick
            path = None
        if path is None:
            s["recent_messages"] = []
            continue
        s["recent_messages"] = read_recent_messages(path, limit=limit)
        newest = newest_record_time(path)
        if newest and _is_later(newest, s.get("last_interacted_at")):
            s["last_interacted_at"] = newest
