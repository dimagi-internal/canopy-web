"""Runner readiness — the 'can I fire a turn' self-assessment reported in the heartbeat.

Two halves:
- proactive: cdp_control.cdp_healthy() — is emdash up with its debug port (the #277/#278
  preflight).
- reactive: a marker file next to the runner's state. A failed turn writes it (with the
  reason); a clean turn clears it. This is how "online but not logged in" — invisible to a
  CDP probe — becomes a not-ready signal. It lives ON DISK so it survives --drain-one's
  one-shot process.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import cdp_control

_MARKER = "not-ready"

# How long a failure keeps a runner out of routing.
#
# The marker used to latch FOREVER: `mark_ok` is only ever called after a turn
# executes, and `claim_next_turn` will not give a not-ready runner a turn — so one
# failure removed a box from the fleet permanently, and the only way back was a
# human deleting a file. Nothing surfaced it either; the runner keeps heartbeating
# and reads ONLINE the whole time.
#
# Observed 2026-08-01: a laptop was shut down mid-turn, the POST that was in
# flight failed with a DNS error, and the box came back online-but-unroutable with
# `runner execute crashed: … nodename nor servname provided`. A network blip
# during shutdown says nothing about whether the box can run anything.
#
# Expiring instead of latching turns permanent exile into retry-with-backoff. A
# genuinely broken runner (logged out — invisible to the CDP probe) simply fails
# its next turn and marks itself again, which is the behaviour you want; a
# transiently broken one heals on its own.
MARKER_TTL_SECONDS = 900


def _marker(cfg) -> Path:
    base = Path(cfg.state_path).parent if getattr(cfg, "state_path", "") else Path.home() / ".canopy"
    return base / _MARKER


def mark_failed(cfg, note: str) -> None:
    """A turn failed — this runner may be unable to fire (auth/health). Record why."""
    try:
        p = _marker(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((note or "recent turn failed")[:200])
    except OSError:
        pass  # best-effort; a missing marker just means "presumed ready"


def mark_ok(cfg) -> None:
    """A turn succeeded — clear any prior failure marker."""
    try:
        _marker(cfg).unlink(missing_ok=True)
    except OSError:
        pass


def compute(cfg) -> tuple[bool, str]:
    """(ready, ready_note). Not ready if emdash's CDP is unreachable, or a recent turn
    failed and hasn't been cleared by a clean run."""
    if not cdp_control.cdp_healthy(port=getattr(cfg, "cdp_port", 9222)):
        return False, "emdash CDP unreachable"
    marker = _marker(cfg)
    try:
        note = marker.read_text().strip()
    except OSError:
        return True, ""
    if not note:
        return True, ""
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        age = 0.0
    if age > MARKER_TTL_SECONDS:
        # Stop holding it against the box. Deleted rather than merely ignored, so
        # the next real failure records a fresh reason instead of resurrecting a
        # stale one.
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
        return True, ""
    return False, note
