"""Should this box install a newer runner right now?

The question has three parts, and each is answered by whoever actually knows:

- **What is installed** — `provenance.code_sha()`, locally. Deliberately NOT the
  server's record of `code_sha`: that is only as fresh as the last heartbeat, and
  a runner that is crash-looping (the case where auto-update matters MOST) has not
  sent one.
- **What should be installed** — `expected_code_sha` off the runner's own row,
  or, when canopy refuses the token, the deployed version off the public health
  check: a box whose sign-in died must still reach the code that recovers it.
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

from . import platform_jobs
# Re-exported so existing callers and tests keep importing it from here.
from .platform_jobs import UPDATER_LABEL  # noqa: F401

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


# --- what the updater last did -----------------------------------------------
#
# The updater is a separate, read-only process that must never heartbeat (see
# the module docstring), so until this existed nothing it decided reached
# canopy-web: a laptop sat behind for five days (shayoni-win-cdp, 2026-10) and
# "deferring while busy", "no build for this platform", "the install failed"
# and "the updater task never runs" all looked identical — a stale code_sha.
# So the updater writes its answer here, and the DAEMON reports it as the
# `update` health check on its heartbeat.

#: The timer runs every 30 min; two missed ticks and a bit means the updater is
#: not running at all (task deleted, laptop asleep for hours, a crashing job).
UPDATER_SILENT_AFTER = 2.5 * 3600
FAILED = "failed"


def _status_path(cfg) -> Path:
    return _busy_path(cfg).with_name("update-status.json")


def record(cfg, status: str, expected: str, *, installed: str = "", detail: str = "",
           now: float | None = None) -> None:
    """Remember the updater's answer for the daemon's heartbeat. Best-effort: a
    failure here must never change what the updater does."""
    try:
        p = _status_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"status": status, "expected": expected or "",
                                 "installed": installed or "", "detail": detail,
                                 "at": time.time() if now is None else now}))
    except OSError:
        pass


def health_check(cfg, *, now: float | None = None) -> dict | None:
    """The `update` health check from the updater's last answer, or None when no
    updater has written one (a box older than this, or one installed without it)."""
    now = time.time() if now is None else now
    # Held boxes skip the check entirely on the shell updaters' path, so their
    # last answer goes old on purpose — say held, not "the updater is not running".
    hold = _status_path(cfg).with_name("hold-updates")
    if hold.exists():
        return {"name": "update", "status": "warn",
                "detail": f"updates are held ({hold}) — delete it to resume"}
    try:
        raw = json.loads(_status_path(cfg).read_text())
        status, at = str(raw["status"]), float(raw["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    expected = str(raw.get("expected") or "")[:12] or "?"
    installed = str(raw.get("installed") or "")[:12] or "?"
    extra = str(raw.get("detail") or "")
    when = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(at))
    if now - at > UPDATER_SILENT_AFTER:
        return {"name": "update", "status": "fail",
                "detail": f"the updater has not run since {when} (last answer: {status}) — "
                          "its scheduled job is missing, failing, or the machine slept"}
    if status == CURRENT:
        result, text = "ok", f"current ({installed})"
    elif status == BUSY:
        result, text = "warn", f"behind: {installed} → {expected}, waiting for turns in flight to finish"
    elif status == STALE:
        result, text = "warn", f"behind: {installed} → {expected}, installing"
    elif status == FAILED:
        result, text = "fail", f"behind: {installed} → {expected}, the install failed"
    else:  # unknown, held, anything newer than this code
        result, text = "warn", f"{status}: installed {installed}, expected {expected}"
    if extra:
        text = f"{text}: {extra}"
    return {"name": "update", "status": result, "detail": f"{text} · checked {when}"[:2000]}


# --- the update doorbell -----------------------------------------------------
#
# The server rings `update_available` down the control channel the moment a
# heartbeat reports a sha that differs from the deployed expectation — on EVERY
# stale beat, so a missed frame costs one beat, not one timer cycle. The daemon
# therefore owns the throttle: one kick per window, not sixty an hour while the
# updater is deliberately deferring (busy).
NUDGE_MIN_SECONDS = 600.0
_last_nudge_at = 0.0

def _kickstart_updater() -> None:
    platform_jobs.kickstart_updater()


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
    # Advance the throttle on the ATTEMPT, not the success: a broken supervisor
    # call would otherwise warn on every ~10s heartbeat, and the 30-min timer is
    # the rescue for that case regardless.
    _last_nudge_at = now
    job = platform_jobs.updater_job_name()   # launchd label, or Task Scheduler path
    try:
        _kickstart_updater()
    except Exception as exc:  # noqa: BLE001 — a supervisor hiccup must not cost the socket
        logger.warning("update nudge: could not kickstart %s: %s", job, exc)
        return False
    logger.info("update nudge: kickstarted %s (expected %s, installed %s)",
                job, expected[:12], installed[:12])
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

    A token canopy REFUSES (401/403) is not a flaky network: it repeats every
    cycle, and the fix for it — newer runner code that recovers the sign-in — is
    exactly what this check would install. So that answer falls back to the
    deployed version the public health check names (canopy-web#1762); before it,
    jj-mbp-cdp logged `unknown` every 30 minutes for nine hours with a dead token.
    """
    from . import provenance

    installed = provenance.code_sha() if installed_sha is None else installed_sha
    try:
        rows = client.list_runners()
    except Exception as exc:  # noqa: BLE001 — a flaky network is not a reason to update
        if getattr(exc, "status", None) in (401, 403):
            expected = client.deployed_runner_sha()
            logger.warning("update check: canopy refused this runner's token (%s); comparing "
                           "with the deployed version %s instead", exc.status, expected[:12] or "?")
            return _compare(cfg, installed, expected, now)
        logger.warning("update check: could not reach the control plane: %s", exc)
        return UNKNOWN, ""

    mine = next((r for r in rows if str(r.get("id")) == str(cfg.runner_id)), None)
    if mine is None:
        # Retired, or not visible to this token. Either way we have no expectation
        # to compare against — and reinstalling would not fix it.
        logger.warning("update check: runner %s is not in the fleet list", cfg.runner_id)
        return UNKNOWN, ""

    return _compare(cfg, installed, (mine.get("expected_code_sha") or "").strip(), now)


def _compare(cfg, installed: str, expected: str, now: float | None) -> tuple[str, str]:
    if not expected or not installed:
        return UNKNOWN, expected
    if expected == installed:
        return CURRENT, expected

    carrying = in_flight(cfg, now=now)
    if carrying:  # a positive count; None (unknown/dead) deliberately does NOT block
        return BUSY, expected
    return STALE, expected
