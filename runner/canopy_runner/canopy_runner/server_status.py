"""What canopy said to this box's last heartbeat, for the menu-bar app.

The local ``heartbeat`` file only proves the loop is turning. A box whose token
canopy revoked keeps turning it every 5s while the server shows it offline and
hands its work elsewhere, so the menu bar read green for hours. This file is
canopy's side of the story: whether the last heartbeat was accepted, when one
last was, and, when refused, the HTTP status and why.

``server-status.json`` beside ``runner.json``; written by ``Client.heartbeat``
alone, so every heartbeat path reports here. Unconfigured (tests, one-shot CLI
commands) it writes nothing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

_path: Path | None = None
_last_ok_at: float | None = None


def configure(path: Path) -> None:
    global _path, _last_ok_at
    _path = path
    # Keep "last accepted" across a restart, so a box that restarts into a
    # revoked token still says how long canopy has been refusing it.
    try:
        _last_ok_at = json.loads(path.read_text()).get("last_ok_at")
    except (OSError, ValueError, AttributeError):
        _last_ok_at = None


def _write(doc: dict) -> None:
    if _path is None:
        return
    try:
        tmp = _path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        tmp.replace(_path)
    except OSError:
        pass


def accepted(reply: dict) -> None:
    global _last_ok_at
    _last_ok_at = time.time()
    _write({"ok": True, "at": _last_ok_at, "last_ok_at": _last_ok_at,
            "paused": bool(reply.get("paused"))})


def refused(exc: Exception) -> None:
    """``status`` is the HTTP code when canopy answered, None when it was not reached."""
    _write({"ok": False, "at": time.time(), "last_ok_at": _last_ok_at,
            "status": getattr(exc, "status", None), "detail": str(exc)[:300]})
