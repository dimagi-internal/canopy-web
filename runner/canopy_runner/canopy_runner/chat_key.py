"""Leave a chat's key where that chat's Claude session — and nothing else — finds it.

canopy mints a key (`canopy_sessions.ChatKey`) when this runner claims a chat's
turn; presented in the `X-Canopy-Chat-Key` header it reaches that chat's secrets
and page and nothing else. A laptop runner does not spawn the session — emdash
does — so it cannot set the session's environment. It writes the key to disk
under the two names the session itself can work out:

* `~/.canopy/chat/task/<emdash task>.key` — the canopy plugin's MCP headers
  helper derives the task from the session's worktree path, as it already does
  for a confined session's profile;
* `~/.canopy/chat/session/<Claude session id>.key` — `canopy secret` reads
  `CLAUDE_CODE_SESSION_ID`, which Claude Code gives every command it runs.

Private to this OS user (dirs 0700, files 0600), and pruned after a week — the
key's own lifetime. Best-effort: a turn still runs if the file cannot be
written; `canopy secret` and the page tools then fall back to the older path.
"""
from __future__ import annotations

import logging
import os
import pathlib
import re
import time

logger = logging.getLogger(__name__)

ROOT = pathlib.Path.home() / ".canopy" / "chat"
KEEP_SECONDS = 7 * 24 * 3600
#: A task name or a Claude session id; anything else is refused rather than
#: joined onto a path.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


def _prune(root: pathlib.Path, now: float) -> None:
    for p in root.glob("*.key"):
        try:
            if now - p.stat().st_mtime > KEEP_SECONDS:
                p.unlink()
        except OSError:
            continue


def write(turn: dict, *, task: str = "", transcript_id: str = "",
          root: pathlib.Path | None = None, now=time.time) -> list[pathlib.Path]:
    """Write this chat turn's key under whichever names are known. Returns the paths."""
    key = str(turn.get("chat_key") or "")
    if not key:
        return []
    base = root or ROOT
    written = []
    for kind, name in (("task", task), ("session", transcript_id)):
        if not name or not _NAME.match(name):
            continue
        d = base / kind
        try:
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
            _prune(d, now())
            path = d / f"{name}.key"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(key)
            written.append(path)
        except OSError as exc:
            logger.warning("could not leave the chat key for %s %s: %s", kind, name, exc)
    return written
