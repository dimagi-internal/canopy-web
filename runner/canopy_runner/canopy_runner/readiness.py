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

from pathlib import Path

from . import cdp_control

_MARKER = "not-ready"


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
    try:
        note = _marker(cfg).read_text().strip()
        if note:
            return False, note
    except OSError:
        pass
    return True, ""
