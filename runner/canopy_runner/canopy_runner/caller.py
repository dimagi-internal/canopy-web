"""Hand the agent the caller envelope: who asked for this turn, and how sure canopy is.

The claim response carries `caller_context` (canopy-web's
`apps/harness/caller_context.py`). The runner writes it to
`~/.canopy/caller/<turn_id>.json` and, for a COMMAND-shaped prompt
(`/ace:turn --thread …`), appends `--caller <path>` so the agent's turn skill
knows where to look. The agent can re-read the same document mid-turn with the
`who_is_asking` MCP tool.

**Never the prompt body.** A chat turn's prompt is the person's own words and
becomes the transcript, so it is left byte-for-byte alone: pasting the envelope
into it would read as something they typed. Only a slash-command prompt — which
no human typed — gets the flag.

**Private to this OS user.** The file names a person and carries what the
workspace knows about them, so the directory is 0700 and each file 0600. Files
older than a week are pruned on each write: a turn that old is long finished,
and `who_is_asking` serves anything a later reader needs.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import time

from . import session_naming

logger = logging.getLogger(__name__)

CALLER_ROOT = pathlib.Path.home() / ".canopy" / "caller"
KEEP_SECONDS = 7 * 24 * 3600

#: A turn id is a UUID; anything else is refused rather than joined onto a path.
_TURN_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


def _prune(root: pathlib.Path, now: float) -> None:
    for p in root.glob("*.json"):
        try:
            if now - p.stat().st_mtime > KEEP_SECONDS:
                p.unlink()
        except OSError:
            continue


def write_caller_file(turn: dict, *, root: pathlib.Path | None = None,
                      now=time.time) -> pathlib.Path | None:
    """Write this turn's envelope; return its path, or None when there is none.

    None is normal for a server older than the envelope, and for a turn id that
    is not a UUID. Best-effort by design: a turn must still run when the file
    cannot be written — the agent then has `who_is_asking` and, failing that,
    its own triage, exactly as before this existed.
    """
    env = turn.get("caller_context")
    tid = str(turn.get("id") or "")
    if not isinstance(env, dict) or not _TURN_ID.match(tid):
        return None
    root = root or CALLER_ROOT
    try:
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        path = root / f"{tid}.json"
        tmp = root / f".{tid}.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(env, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
        _prune(root, now())
        return path
    except OSError:
        logger.warning("could not write the caller envelope for turn %s", tid, exc_info=True)
        return None


def with_caller_flag(prompt: str, path: pathlib.Path | None) -> str:
    """`/ace:turn --thread X` -> `/ace:turn --thread X --caller <path>`.

    Only a slash command, and only once. A free-text prompt is somebody's words.
    """
    if path is None or not prompt.startswith("/") or " --caller " in prompt:
        return prompt
    first, sep, rest = prompt.partition("\n")
    return f"{first} --caller {path}{sep}{rest}"


# --- the pending pointer: who is asking, for a FREE-TEXT prompt -----------------------
#
# A chat prompt (Slack, the web chat, the widget) is the person's own words, so it
# never carries `--caller`. The agent still has to KNOW who it is talking to — on
# 2026-09-26 Hal, driven from Slack by a non-member, pushed and deployed code with an
# envelope on disk that said relationship=caller, verified=false, turn_mode=manual,
# and never read it. So before the runner types a turn's text into a session it
# leaves a POINTER keyed by that session's emdash task name, and the canopy plugin's
# UserPromptSubmit hook (`caller_context.py`) consumes it and adds a short summary of
# the envelope to Claude's context — beside the prompt, never inside it.
#
# Keyed by TASK NAME because it is the one handle both ends have: the runner knows it
# before it sends (it names the task, or reuse hands it one), and the hook derives it
# from its own transcript path — the same anchor `profile_guard` confines cx- sessions
# by (`…-worktrees-<repo>-emdash-<task>-<suffix>`). The worktree path would be the
# obvious key, but a NEW session's worktree does not exist until emdash creates it,
# which is the same click that submits the prompt.
#
# One-shot and short-lived: the hook consumes the pointer on the first prompt it
# sees, and ignores one older than `PENDING_FRESH_SECONDS` — a pointer the send never
# followed (a collision, a failed send) must not attach a stranger's name to the next
# thing a human types into that session.

PENDING_FRESH_SECONDS = 120
_TASK_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


def pending_root() -> pathlib.Path:
    # Derived at call time so a test (or anything) that moves CALLER_ROOT moves this too.
    return CALLER_ROOT / "pending"


def write_pending(task: str, turn: dict, envelope: pathlib.Path | None, *,
                  root: pathlib.Path | None = None, now=time.time) -> pathlib.Path | None:
    """Leave the hook a pointer to this turn's envelope, keyed by `task`. Best-effort.

    None — and nothing written — when there is no envelope, or `task` is not a name
    the hook could have derived. A failure here costs the agent the summary, never
    the turn: `who_is_asking` and the envelope file are still there.
    """
    if envelope is None or not task or not _TASK_KEY.match(task):
        return None
    tid = str(turn.get("id") or "")
    if not _TURN_ID.match(tid):
        return None
    root = root or pending_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        for p in root.glob("*.json"):           # prune pointers nobody consumed
            try:
                if now() - p.stat().st_mtime > KEEP_SECONDS:
                    p.unlink()
            except OSError:
                continue
        path = root / f"{task}.json"
        tmp = root / f".{task}.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"version": 1, "turn_id": tid, "task": task,
                       "envelope": str(envelope), "written_at": now()}, fh)
        os.replace(tmp, path)
        return path
    except OSError:
        logger.warning("could not leave the caller pointer for task %s", task, exc_info=True)
        return None


def clear_pending(task: str, *, root: pathlib.Path | None = None) -> None:
    """Withdraw a pointer whose prompt was NOT delivered. Never raises."""
    if not task or not _TASK_KEY.match(task):
        return
    try:
        ((root or pending_root()) / f"{task}.json").unlink()
    except OSError:
        pass


# --- restricted execution: a caller's turn, confined to its capability ---------------
#
# canopy-web decides per turn whether the asker gets the agent's FULL profile (its
# owner, an admin, canopy itself) or is a CALLER confined to a capability from the
# agent's declared interface. For a caller's turn this runner:
#   * opens it in its OWN emdash session — a distinct thread key and a `cx-` task
#     name — so it never shares a session with an admin's work;
#   * writes the capability's profile to ~/.canopy/profiles/<task>.json BEFORE the
#     session starts, where canopy's `profile_guard` hook reads it on every tool call;
#   * starts it with the capability's `entry` command rather than the admin's turn.

PROFILE_ROOT = pathlib.Path.home() / ".canopy" / "profiles"

#: The profile contract this runner implements. Reported only when the installed
#: canopy plugin's guard implements the same one — see `profiles_supported`.
PROFILES_VERSION = 1
_MARKER = re.compile(r"^PROFILE_ENFORCEMENT_VERSION\s*=\s*(\d+)", re.M)
_supported_cache: tuple[float, int] | None = None


class ProfileError(RuntimeError):
    """A caller's turn cannot be confined, so it must not run."""


def capability(turn: dict) -> dict | None:
    """The capability profile when this is a caller's turn, else None (full profile)."""
    env = turn.get("caller_context") or {}
    if env.get("profile") != "restricted":
        return None
    cap = env.get("capability")
    # A restricted envelope with no capability is still restricted: deny-all.
    return cap if isinstance(cap, dict) else {"name": "none", "tools": [], "bash": [],
                                               "read_paths": [], "write_paths": [], "entry": None}


def thread_id(turn: dict) -> str:
    env = turn.get("caller_context") or {}
    conv = env.get("conversation") or {}
    ref = turn.get("origin_ref") or {}
    return str(conv.get("thread_id") or ref.get("thread_id") or "")


def entry_prompt(turn: dict, prompt: str) -> str:
    """A caller's session starts with the capability's `entry`, never the admin's turn.

    `{thread_id}` is filled from the conversation; an entry that needs one on a
    turn that has none is refused rather than run with a hole in it.
    """
    cap = capability(turn)
    if cap is None or not cap.get("entry"):
        return prompt
    entry = str(cap["entry"])
    if "{thread_id}" in entry:
        tid = thread_id(turn)
        if not tid:
            raise ProfileError(f"capability '{cap.get('name')}' starts on a thread, "
                               "and this turn has none")
        entry = entry.replace("{thread_id}", tid)
    return entry


def write_profile(task: str, turn: dict, *, root: pathlib.Path | None = None) -> pathlib.Path:
    """Write the profile the guard confines `task`'s session to. Raises ProfileError.

    Called BEFORE the session is created or sent to, so its first tool call is
    already confined. Failing to write is fatal for the turn: the guard would deny
    everything anyway, and a turn that silently does nothing is worse than one that
    fails and says why.
    """
    cap = capability(turn)
    if cap is None:
        raise ProfileError("not a caller's turn")
    if not session_naming.is_restricted_task(task) or not re.fullmatch(r"cx-[a-z0-9-]{1,200}", task):
        raise ProfileError(f"a caller's turn must run in a cx- session, not {task!r}")
    root = root or PROFILE_ROOT
    doc = {"version": PROFILES_VERSION, "turn_id": str(turn.get("id") or ""),
           "thread_id": thread_id(turn) or None, "capability": cap,
           # The envelope the session is told to read (`--caller`); naming it here
           # lets the guard allow exactly that one file outside the worktree.
           "caller_path": str(CALLER_ROOT / f"{turn.get('id')}.json"),
           # canopy's MCP credential for THIS session — the caller, scoped to the
           # capability — sent by the canopy plugin's headers helper in place of the
           # owner's PAT. The session cannot read this file (profile_guard), so it
           # cannot lift the token; a missing one makes the helper send an invalid
           # header, never the PAT.
           "mcp_token": turn.get("mcp_token") or None,
           }
    try:
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        tmp = root / f".{task}.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
        path = root / f"{task}.json"
        os.replace(tmp, path)
        return path
    except OSError as exc:
        raise ProfileError(f"could not write the session profile: {exc}") from exc


def _canopy_plugin_root() -> pathlib.Path | None:
    try:
        d = json.loads((pathlib.Path.home() / ".claude" / "plugins" /
                        "installed_plugins.json").read_text())
        return pathlib.Path(d["plugins"]["canopy@canopy"][0]["installPath"])
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def profiles_supported(*, now=time.monotonic, plugin_root=None) -> int:
    """PROFILES_VERSION when the installed canopy plugin enforces it, else 0.

    This is what the runner tells canopy-web on every heartbeat, and 0 means
    "never give me a caller's turn". The runner can open a `cx-` session and write
    its profile, but only canopy's `profile_guard` hook makes the profile binding —
    so reporting support without it would route callers into the full profile.
    Cached for a minute: it is read on every beat.
    """
    global _supported_cache
    t = now()
    if plugin_root is None and _supported_cache and t - _supported_cache[0] < 60:
        return _supported_cache[1]
    root = plugin_root or _canopy_plugin_root()
    version = 0
    if root is not None:
        try:
            guard = (root / "hooks" / "profile_guard.py").read_text()
            hooks = (root / "hooks" / "hooks.json").read_text()
            m = _MARKER.search(guard)
            if m and int(m.group(1)) >= PROFILES_VERSION and "profile_guard.py" in hooks:
                version = PROFILES_VERSION
        except OSError:
            version = 0
    if plugin_root is None:
        _supported_cache = (t, version)
    return version
