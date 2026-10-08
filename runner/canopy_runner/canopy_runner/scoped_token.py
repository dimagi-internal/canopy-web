"""Leave a caller's canopy token where that session — and nothing else — finds it.

A FULL-profile turn asked by someone who is not the agent's owner, an admin or
canopy (a `full:` rule, a workspace editor) is not confined to a capability, but
its canopy tools must still run as the ASKER, never as this box's owner
(who-is-asking phase 5, canopy-web#1332). canopy hands such a turn a caller token
with the claim (`mcp_token`, `scoped` server-side).

A laptop runner does not spawn the session — emdash does — so it cannot set the
session's environment. It writes the token to
`~/.canopy/scoped/task/<emdash task>.token`, which the canopy plugin's MCP headers
helper derives from the session's worktree path exactly as it does the chat key.
A confined session's token travels in its profile instead (`caller.write_profile`).

Two rules the helper depends on:

* a task that is reused for a later turn with NO scoped token must not keep the
  last one — `write` removes the file in that case, or an owner's turn would
  present a colleague's (now stale) credential;
* a write that fails is the caller's problem: the turn must not run on the box's
  PAT, so `write` raises and the runner fails the turn.

Private to this OS user (dirs 0700, files 0600), pruned after a week — the
token's own lifetime.
"""
from __future__ import annotations

import os
import pathlib
import re
import time

ROOT = pathlib.Path.home() / ".canopy" / "scoped"
KEEP_SECONDS = 7 * 24 * 3600
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


class ScopedTokenError(RuntimeError):
    """The token could not be left where the session finds it."""


def _prune(root: pathlib.Path, now: float) -> None:
    for p in root.glob("*.token"):
        try:
            if now - p.stat().st_mtime > KEEP_SECONDS:
                p.unlink()
        except OSError:
            continue


def write(turn: dict, *, task: str, root: pathlib.Path | None = None,
          now=time.time) -> pathlib.Path | None:
    """Leave this turn's scoped token for `task`'s session, or clear a stale one.

    Only for a turn that is NOT confined (its token travels in the profile).
    Returns the path written, None when the turn carries no scoped token.
    Raises ScopedTokenError when a token could not be left."""
    token = str(turn.get("mcp_token") or "")
    if not task or not _NAME.match(task):
        if token:
            raise ScopedTokenError(f"{task!r} is not a task name a canopy token can be left for")
        return None
    d = (root or ROOT) / "task"
    path = d / f"{task}.token"
    try:
        if not token:
            path.unlink(missing_ok=True)
            return None
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        _prune(d, now())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token)
        return path
    except OSError as exc:
        raise ScopedTokenError(f"could not leave the session's canopy token: {exc}") from exc

