"""Install canopy's tool-lifecycle hooks into the USER-level Claude Code settings.

User level (`~/.claude/settings.json`), deliberately, for two reasons:

1. **One install covers every session on the machine** — every emdash worktree
   and every `claude -p` — with no per-session setup. Verified 2026-07-27: a
   single user-level hook captured events from two concurrent sessions in
   different worktrees at once.
2. **emdash owns the project-level file.** Its worktrees carry a
   `.claude/settings.local.json` that emdash writes and rewrites, with its own
   hooks pointing at its own loopback port. Writing there would fight it.
   emdash uses only `UserPromptSubmit` / `Notification` / `Stop`, so canopy's
   tool events compose with them rather than colliding.

The command mirrors emdash's own idiom (`curl -sf … || true`), which is the
established pattern on these machines: silent, short-timeout, and incapable of
failing the hook. That matters most for `PreToolUse`, which CAN block a tool
call — ours cannot, because it never returns a decision and gives up after two
seconds. It matters for `PostToolUse` too: a hook that hangs slows every tool
call the agent makes, whether or not it can deny one.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("canopy_runner.hooks")

# Both halves of the lifecycle. PreToolUse gives "started" the instant a call
# begins, PostToolUse gives the result — together they turn the view from
# "something happened" into "it is running `npm test` right now".
#
# Safe despite PreToolUse being able to block a tool call: our hook is
# fire-and-forget with a hard 2s cap and never returns a decision, so it has no
# mechanism to deny or stall one.
# Tool lifecycle plus turn boundaries. UserPromptSubmit/Stop are what make a
# session read as WORKING before its first tool call — while Claude is thinking,
# nothing else fires at all.
#
# emdash also hooks UserPromptSubmit and Stop, at PROJECT level, pointing at its
# own port. Both run: Claude Code executes every matching hook, and ours is
# additive rather than a replacement.
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")
# Marks the entry as ours so install/remove are idempotent and we never touch
# a hook somebody else put there.
MARKER = "canopy-hook-listener"

# Two seconds is far above the local round trip (the listener answers before it
# does any work) and far below anything a human would notice on a tool call.
CURL_MAX_TIME = 2


def hook_command(port: int, nonce: str) -> str:
    """The shell command Claude Code runs for each tool call.

    `-d @-` forwards the hook JSON verbatim on stdin — no parsing in the shell.
    `-sf`, `--max-time` and `|| true` together guarantee the hook cannot fail,
    hang, or print, whatever state canopy is in.
    """
    return (
        f"curl -sf --max-time {CURL_MAX_TIME} -X POST "
        f'-H "Content-Type: application/json" '
        f'-H "X-Canopy-Token: {nonce}" '
        f'-d @- "http://127.0.0.1:{port}/hook" || true  # {MARKER}'
    )


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _is_ours(entry: dict) -> bool:
    return any(MARKER in str(h.get("command", ""))
               for h in entry.get("hooks", []) if isinstance(h, dict))


def install(settings_path: Path, *, port: int, nonce: str) -> bool:
    """Add (or refresh) canopy's hook. Returns True if the file was written.

    Idempotent: an existing canopy entry is REPLACED, so a changed port or a
    rotated nonce takes effect without accumulating stale entries. Hooks from
    anything else — emdash's, the user's own — are preserved untouched.
    """
    settings = _load(settings_path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        logger.warning("hooks in %s is not an object; refusing to touch it", settings_path)
        return False
    for event in HOOK_EVENTS:
        entries = [e for e in hooks.get(event, [])
                   if isinstance(e, dict) and not _is_ours(e)]
        entries.append({"hooks": [{"type": "command",
                                   "command": hook_command(port, nonce)}]})
        hooks[event] = entries
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    except OSError as exc:
        logger.warning("could not write %s: %s", settings_path, exc)
        return False
    return True


def remove(settings_path: Path) -> bool:
    """Remove canopy's hook, leaving every other hook in place.

    The counterpart to `install` — a runner configured with `hook_port = 0`
    calls this, so turning the feature off actually un-installs rather than
    leaving a dangling curl to a port nothing is listening on.
    """
    settings = _load(settings_path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in HOOK_EVENTS:
        existing = hooks.get(event, [])
        kept = [e for e in existing if isinstance(e, dict) and not _is_ours(e)]
        if len(kept) == len(existing):
            continue  # nothing of ours under this event
        changed = True
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if not changed:
        return False
    try:
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    except OSError as exc:
        logger.warning("could not write %s: %s", settings_path, exc)
        return False
    return True
