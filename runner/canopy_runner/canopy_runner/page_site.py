"""Mark a chat held on a Connected site's page, where its session's own code can see it.

canopy-web sends `page_site` (`{site, withheld}`) with a turn whose conversation is
held on a site's page that its agent serves: there the agent reaches the site ONLY
as the person who sent, through canopy's `site_tools` / `site_call` — never with its
own login at the site (`withheld`, the agent's `own_login:` credential names).

A cloud runner spawns the session, so it keeps those names out of the turn's
environment and sets `CANOPY_PAGE_SITE`. A laptop runner does not spawn it — emdash
does — so it cannot. It leaves a marker where the agent's own MCP headers helper can
find it from `EMDASH_TASK_NAME` (which emdash exports into the session and Claude
Code passes to a helper):

    ~/.canopy/page/task/<emdash task>.json   {"site": ..., "withheld": [...]}

The helper then sends no credential for that site. This is weaker than the cloud
runner's withholding — the agent's own `.env` is still on this disk, and a session
that reads it directly is stopped only by its instructions (the caller envelope's
`site` block) — and is said so in docs/architecture/runners.md.

Private to this OS user (dir 0700, file 0600), pruned after a week. Best-effort: a
turn still runs if the marker cannot be written.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import time

logger = logging.getLogger(__name__)

ROOT = pathlib.Path.home() / ".canopy" / "page"
KEEP_SECONDS = 7 * 24 * 3600
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
_SITE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,100}$")


def of(turn: dict) -> dict | None:
    """The turn's `page_site` block, validated, or None."""
    block = turn.get("page_site")
    if not isinstance(block, dict):
        return None
    site = str(block.get("site") or "").strip()
    if not _SITE.match(site):
        return None
    names = block.get("withheld") if isinstance(block.get("withheld"), list) else []
    return {"site": site, "withheld": sorted({str(n) for n in names if _ENV_NAME.match(str(n))})}


def _prune(d: pathlib.Path, now: float) -> None:
    for p in d.glob("*.json"):
        try:
            if now - p.stat().st_mtime > KEEP_SECONDS:
                p.unlink()
        except OSError:
            continue


def write(turn: dict, *, task: str, root: pathlib.Path | None = None,
          now=time.time) -> pathlib.Path | None:
    """Leave (or clear) the marker for `task`. Returns the path written, else None."""
    if not task or not _NAME.match(task):
        return None
    d = (root or ROOT) / "task"
    path = d / f"{task}.json"
    block = of(turn)
    try:
        if block is None:
            if path.exists():
                path.unlink()
            return None
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        _prune(d, now())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(block, fh)
        return path
    except OSError as exc:
        logger.warning("could not leave the page-site marker for task %s: %s", task, exc)
        return None
