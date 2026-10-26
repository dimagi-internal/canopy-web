"""The box's usage report: what its Claude Code sessions spent, sent to canopy-web.

Per Claude session, per UTC day — tokens and a little identity (repo, when),
never content — from `canopy_transcript.scan_usage`, scoped to
the repos canopy-web names for this box (`usage_scope`). Rides the report sweep:
every `INTERVAL_SECONDS`, only transcripts that changed since the last report are
read.

Scanning, chunking and remembering what was sent are `canopy_transcript`'s
(`send_usage_report`), shared with the cloud runner.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from canopy_transcript import send_usage_report

logger = logging.getLogger(__name__)

#: How often to report; a failure is retried after `RETRY_SECONDS`.
INTERVAL_SECONDS = 600
RETRY_SECONDS = 60

_state = {"scan_at": 0.0}


def maybe_report(cfg, client, *, now_fn=time.time, home: Path | None = None) -> bool:
    """Report when due. Never raises into the tick; a failure is retried after
    `RETRY_SECONDS`."""
    now = now_fn()
    if now < _state["scan_at"]:
        return False
    _state["scan_at"] = now + RETRY_SECONDS
    try:
        scope = client.usage_scope(cfg.runner_id)
    except Exception as exc:  # noqa: BLE001 — a report never breaks the tick
        logger.debug("usage scope unavailable (%s)", exc)
        return False
    try:
        n = send_usage_report(lambda chunk: client.post_usage(cfg.runner_id, chunk),
                              home=home or Path.home(), repos=scope.get("repos") or [])
    except Exception:  # noqa: BLE001
        logger.warning("usage report failed; will retry", exc_info=True)
        return False
    _state["scan_at"] = now + INTERVAL_SECONDS
    if n:
        logger.info("usage report: %d session(s)", n)
    return True
