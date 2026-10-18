"""The box's usage report: what its Claude Code sessions spent, sent to canopy-web.

Per Claude session, per UTC day — tokens and a little identity (repo, first prompt
as a title, when), never content — from `canopy_transcript.scan_usage`, scoped to
the repos canopy-web names for this box (`usage_scope`). Rides the report sweep:
the scope is read every `SCOPE_SECONDS`, so a person's "Check for missing sessions"
(a full rescan) is answered within about a minute; otherwise only transcripts that
changed since the last report are read, every `INTERVAL_SECONDS`.

Scanning, chunking and remembering what was sent are `canopy_transcript`'s
(`send_usage_report`), shared with the cloud runner.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from canopy_transcript import send_usage_report

logger = logging.getLogger(__name__)

SCOPE_SECONDS = 60
INTERVAL_SECONDS = 600

_state = {"scope_at": 0.0, "scan_at": 0.0}


def maybe_report(cfg, client, *, now_fn=time.time, home: Path | None = None) -> bool:
    """Report when due, or at once when a full rescan was asked for. Never raises
    into the tick; a failure is retried at the next scope read."""
    now = now_fn()
    if now < _state["scope_at"]:
        return False
    _state["scope_at"] = now + SCOPE_SECONDS
    try:
        scope = client.usage_scope(cfg.runner_id)
    except Exception as exc:  # noqa: BLE001 — a report never breaks the tick
        logger.debug("usage scope unavailable (%s)", exc)
        return False
    full = bool(scope.get("full"))
    if not full and now < _state["scan_at"]:
        return False
    try:
        n = send_usage_report(lambda chunk, last: client.post_usage(cfg.runner_id, chunk, full=last),
                              home=home or Path.home(), repos=scope.get("repos") or [], full=full)
    except Exception:  # noqa: BLE001
        logger.warning("usage report failed; will retry", exc_info=True)
        return False
    _state["scan_at"] = now + INTERVAL_SECONDS
    if n or full:
        logger.info("usage report: %d session(s)%s", n, " (full rescan)" if full else "")
    return True
