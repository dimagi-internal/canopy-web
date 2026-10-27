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
        # UTF-8, never the locale's: a note holding a character cp1252 lacks raised
        # UnicodeEncodeError (not an OSError) inside execute's failure path.
        p.write_text((note or "recent turn failed")[:200], encoding="utf-8")
    except OSError:
        pass  # best-effort; a missing marker just means "presumed ready"


def mark_signed_out(cfg) -> bool:
    """This box's Claude login is gone. Reported as not ready with the note the
    server keys on (`canopy_transcript.SIGNED_OUT_NOTE`): it claims nothing, and
    a message waiting on it says why. True when it was not already marked, so the
    caller raises its alert once, not on every hook."""
    from canopy_transcript import SIGNED_OUT_NOTE

    already = is_signed_out(cfg)
    try:
        p = _marker(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SIGNED_OUT_NOTE + " — sign in with `claude /login` there", encoding="utf-8")
    except OSError:
        return False
    return not already


def is_signed_out(cfg) -> bool:
    from canopy_transcript import SIGNED_OUT_NOTE

    try:
        return _marker(cfg).read_text(encoding="utf-8", errors="replace").startswith(SIGNED_OUT_NOTE)
    except OSError:
        return False


def clear_signed_out(cfg) -> bool:
    """Claude answered something on this box, so its login works again. Clears
    only a signed-out mark — never another failure's. True when it cleared one."""
    if not is_signed_out(cfg):
        return False
    mark_ok(cfg)
    return True


def mark_ok(cfg) -> None:
    """A turn succeeded — clear any prior failure marker."""
    try:
        _marker(cfg).unlink(missing_ok=True)
    except OSError:
        pass


def compute(cfg) -> tuple[bool, str]:
    """(ready, ready_note). Not ready if the runtime cannot start a session (emdash's
    CDP unreachable; claude-cli without its CLI or adapter), or a recent turn failed
    and hasn't been cleared by a clean run."""
    from . import desktop

    if desktop.needs_emdash() and not cdp_control.cdp_healthy(port=getattr(cfg, "cdp_port", 9222)):
        return False, "emdash CDP unreachable"

    if desktop.current() == desktop.CLAUDE_DESKTOP and desktop.claude_cli() is None:
        # Switched to Claude desktop but cannot open a session there: new threads
        # fall back to emdash (desktop.maybe_execute). Said here so it shows on the
        # runner, not only in a log nobody reads.
        return False, ("runtime is Claude desktop but no Claude Code CLI was found "
                       "(PATH, ~/.local/bin, Homebrew, the app's bundled copy) — new sessions open in emdash")
    if desktop.current() == desktop.CLAUDE_CLI:
        from canopy_acp import ADAPTER_PACKAGE

        from . import headless

        if desktop.claude_cli() is None or headless.adapter_argv() is None:
            return False, ("runtime is claude-cli but the Claude Code CLI or the ACP adapter "
                           f"({ADAPTER_PACKAGE}) is missing — `canopy-runner runtime set "
                           "claude-cli` installs the adapter")
    marker = _marker(cfg)
    try:
        note = marker.read_text(encoding="utf-8", errors="replace").strip()
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
