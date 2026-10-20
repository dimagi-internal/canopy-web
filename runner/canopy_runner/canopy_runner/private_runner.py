"""This box as its owner's PRIVATE runner — set up from the heartbeat, no CLI (canopy-web#1705).

When the runner's owner marks it private on canopy-web (the runner's page → Private
runner), canopy-web links a private-runner registration to it and reports its id on
every heartbeat. This writes it to ``~/.canopy/private-runner.json`` — the file the
canopy plugin's SessionStart hook reads — so a Claude Code session on this machine that
uses an agent is recorded as the owner's local turn, judged at the role the runner acts
as, without anyone running ``canopy private-runner register``.

Only a file this module wrote is ever replaced or removed (``written_by``): a person's
own ``canopy private-runner register`` stays theirs. Turning the runner shared again
removes the file it wrote, so local sessions stop being recorded here.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

WRITTEN_BY = "canopy-runner"


def registration_file() -> Path:
    return Path.home() / ".canopy" / "private-runner.json"


def _read(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, path)


def observe(me: dict | None, *, base_url: str, path: Path | None = None) -> None:
    """Keep the registration file in step with what canopy-web reports. An answer
    without the fields (an old server) changes nothing."""
    if not isinstance(me, dict) or "private" not in me:
        return
    path = path or registration_file()
    current = _read(path)
    ours = current.get("written_by") == WRITTEN_BY
    pr_id = str(me.get("private_runner_id") or "")
    if me.get("private") and pr_id:
        if current and not ours:
            return  # the person registered this machine themselves: theirs to keep
        doc = {"id": pr_id, "name": me.get("name", ""), "host": me.get("host", ""),
               "runner_id": str(me.get("id") or ""), "base_url": base_url,
               "acts_as": me.get("acts_as") or "", "written_by": WRITTEN_BY}
        if {k: current.get(k) for k in doc} != doc:
            _write(path, {**doc, "registered_at": time.time()})
            logger.info("private runner: local sessions here are recorded as %s's local turns%s",
                        "the owner", f", acting as {doc['acts_as']}" if doc["acts_as"] else "")
    elif not me.get("private") and ours:
        try:
            path.unlink()
            logger.info("private runner: back to a shared runner; local sessions no longer recorded")
        except OSError:
            pass
