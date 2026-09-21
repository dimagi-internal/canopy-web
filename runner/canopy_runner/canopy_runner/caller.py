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
