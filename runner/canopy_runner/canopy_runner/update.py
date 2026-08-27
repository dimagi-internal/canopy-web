"""Should this box install a newer runner right now?

The question has three parts, and each is answered by whoever actually knows:

- **What is installed** — `provenance.code_sha()`, locally. Deliberately NOT the
  server's record of `code_sha`: that is only as fresh as the last heartbeat, and
  a runner that is crash-looping (the case where auto-update matters MOST) has not
  sent one.
- **What should be installed** — `expected_code_sha` off the runner's own row.
  This is the sha of the runner source in the DEPLOYED image, so it has already
  been through CI, the merge queue and a deploy. Tracking `origin/main` instead
  would install code nothing has deployed AND leave the box permanently
  mismatched against the server, i.e. the staleness banner would fire forever on
  exactly the boxes that are auto-updating correctly.
- **Whether now is a safe moment** — the local in-flight marker the running
  daemon writes each tick. An update restarts the daemon, and a chat turn is
  bridged across ticks, so restarting mid-turn strands a reply.

None of this reaches a box whose macOS account is logged out: the updater is a
LaunchAgent in the same login session as the daemon, so nothing here runs at all.
See com.canopy.runner.updater.plist.template § THE BOUNDARY OF THAT INDEPENDENCE
— that case is caught by the supervisor's dark-runner banner, not by this file.

Read-only by construction: this asks the control plane via GET, and must never
heartbeat. A heartbeat from this second process would stamp the runner ONLINE and
overwrite the provenance the real daemon reports — the updater would be forging
liveness for a daemon that might be dead.

See docs/superpowers/specs/2026-07-28-runner-as-installed-package-design.md.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("canopy_runner.update")

CURRENT = "current"
STALE = "stale"
BUSY = "busy"
UNKNOWN = "unknown"

# How old the in-flight marker may be before we stop believing it. The loop
# rewrites it every tick (poll_seconds defaults to 5), so a marker older than
# this means the daemon is not running its loop at all — stopped, wedged, or
# crash-looping. That is NOT "busy": it is the case auto-update exists to
# rescue, so it must not be allowed to block the update forever.
BUSY_MARKER_MAX_AGE = 120.0


def _busy_path(cfg) -> Path:
    base = Path(cfg.state_path).parent if getattr(cfg, "state_path", "") else Path.home() / ".canopy"
    return base / "in-flight"


def mark_busy(cfg, count: int) -> None:
    """Record how many turns this runner is carrying. Best-effort — a failure here
    must never affect the turn itself."""
    try:
        p = _busy_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"count": int(count), "at": time.time()}))
    except OSError:
        pass


def in_flight(cfg, *, now: float | None = None) -> int | None:
    """Turns in flight per the marker, or None when the marker can't be trusted
    (missing, unreadable, or stale — see BUSY_MARKER_MAX_AGE)."""
    now = time.time() if now is None else now
    try:
        raw = json.loads(_busy_path(cfg).read_text())
        if now - float(raw["at"]) > BUSY_MARKER_MAX_AGE:
            return None
        return int(raw["count"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


# --- the update doorbell -----------------------------------------------------
#
# The server rings `update_available` down the control channel the moment a
# heartbeat reports a sha that differs from the deployed expectation — on EVERY
# stale beat, so a missed frame costs one beat, not one timer cycle. The daemon
# therefore owns the throttle: one kick per window, not sixty an hour while the
# updater is deliberately deferring (busy).
NUDGE_MIN_SECONDS = 600.0
_last_nudge_at = 0.0

UPDATER_LABEL = "com.canopy.runner.updater"


def _kickstart_updater() -> None:
    import os
    import subprocess

    subprocess.run(
        ["launchctl", "kickstart", f"gui/{os.getuid()}/{UPDATER_LABEL}"],
        capture_output=True, timeout=15, check=True,
    )


def nudge(cfg, expected_sha: str, *, now: float | None = None) -> bool:
    """The `update_available` doorbell: kickstart the SEPARATE updater job now
    instead of waiting out its 30-minute timer.

    Never installs in-process — the updater re-checks staleness and the
    in-flight marker itself, so busy deferral, the read-only check and the
    crash-loop rescue are inherited, not re-implemented. Skips when the frame
    raced an install that already happened (expected == installed). Runs on the
    wake-listener thread, so it must never raise: that socket also carries
    cancel and wake."""
    global _last_nudge_at
    from . import provenance

    now = time.time() if now is None else now
    expected = (expected_sha or "").strip()
    installed = provenance.code_sha()
    # Empty on either side is UNKNOWN, never "stale" — the same rule
    # update_status applies on the timer path.
    if not expected or not installed or expected == installed:
        return False
    if now - _last_nudge_at < NUDGE_MIN_SECONDS:
        return False
    # Advance the throttle on the ATTEMPT, not the success: a broken launchctl
    # would otherwise warn on every ~10s heartbeat, and the 30-min timer is the
    # rescue for that case regardless.
    _last_nudge_at = now
    try:
        _kickstart_updater()
    except Exception as exc:  # noqa: BLE001 — a launchctl hiccup must not cost the socket
        logger.warning("update nudge: could not kickstart %s: %s", UPDATER_LABEL, exc)
        return False
    logger.info("update nudge: kickstarted %s (expected %s, installed %s)",
                UPDATER_LABEL, expected[:12], installed[:12])
    return True


def update_status(cfg, client, *, installed_sha: str | None = None,
                  now: float | None = None) -> tuple[str, str]:
    """(status, expected_sha).

    - `current` — installed matches what the deployed server expects.
    - `stale`   — they differ and nothing is in flight: install `expected_sha`.
    - `busy`    — they differ but a turn is in flight: try again next cycle.
    - `unknown` — either side can't be determined. Do nothing. Empty means
                  UNKNOWN, never "different": a dev server bakes in no
                  expectation, and auto-installing an empty sha would be a
                  reinstall loop against a target that does not exist.
    """
    from . import provenance

    installed = provenance.code_sha() if installed_sha is None else installed_sha
    try:
        rows = client.list_runners()
    except Exception as exc:  # noqa: BLE001 — a flaky network is not a reason to update
        logger.warning("update check: could not reach the control plane: %s", exc)
        return UNKNOWN, ""

    mine = next((r for r in rows if str(r.get("id")) == str(cfg.runner_id)), None)
    if mine is None:
        # Retired, or not visible to this token. Either way we have no expectation
        # to compare against — and reinstalling would not fix it.
        logger.warning("update check: runner %s is not in the fleet list", cfg.runner_id)
        return UNKNOWN, ""

    expected = (mine.get("expected_code_sha") or "").strip()
    if not expected or not installed:
        return UNKNOWN, expected
    if expected == installed:
        return CURRENT, expected

    carrying = in_flight(cfg, now=now)
    if carrying:  # a positive count; None (unknown/dead) deliberately does NOT block
        return BUSY, expected
    return STALE, expected
