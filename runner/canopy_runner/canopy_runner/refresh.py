"""Refresh on request — the laptop half of the runner page's Refresh (canopy-web#1763).

A cloud box refreshes by restarting, because its bootstrap is what updates its plugins,
the canopy CLI and Claude Code (cloud_runner `maybe_self_refresh`). A laptop has no
bootstrap to re-run: Claude Code, the plugins and the CLI are its person's own installs.
So a refresh here is the prepare those installs need, done in place: pull every plugin
marketplace, update each plugin installed for this account, and bring the canopy CLI to
the plugin's version (`cli_refresh.check`). Sessions already open keep what they loaded;
the next one gets the new plugins.

The request is read off the heartbeat reply (`refresh_pending`) and discharged by
reporting a `health.bootstrapped_at` newer than it — the contract a cloud box keeps, so
the button and its "Refresh requested" state mean one thing on every runner. What the
refresh did rides along as one health check, so a plugin that would not update shows
on the runner's page instead of only in this log.

In the background, one at a time, and at most once per MIN_INTERVAL_SECONDS, so a
refresh that cannot finish is never a loop. Never takes the runner down.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 1800
TIMEOUT = 300

Runner = Callable[..., subprocess.CompletedProcess]

_lock = threading.Lock()
_running = False
_last_started = 0.0
#: The health block to report once a refresh has finished; None until one has.
_report: dict | None = None


def observe(me: dict | None, **kw) -> threading.Thread | None:
    """Start a refresh when the heartbeat reply says one is pending."""
    if not isinstance(me, dict) or not me.get("refresh_pending"):
        return None
    return start(**kw)


def start(*, now: float | None = None, run: Runner = subprocess.run,
          home: Path | None = None) -> threading.Thread | None:
    global _running, _last_started
    now = time.time() if now is None else now
    with _lock:
        if _running or now - _last_started < MIN_INTERVAL_SECONDS:
            return None
        _running, _last_started = True, now
    t = threading.Thread(target=_run, kwargs={"run": run, "home": home}, daemon=True,
                         name="canopy-refresh")
    t.start()
    return t


def health() -> dict | None:
    """The `health` block for a heartbeat, once a refresh has finished."""
    return _report


def _run(*, run: Runner, home: Path | None) -> None:
    global _running, _report
    try:
        logger.info("refresh: canopy asked for one; updating plugins and the canopy CLI")
        check = prepare(run=run, home=home)
        done = time.time()
        _report = {"checks": [check], "checked_at": done, "bootstrapped_at": done,
                   "refresh_floor_until": _last_started + MIN_INTERVAL_SECONDS}
        logger.info("refresh: done — %s", check["detail"])
    except Exception:  # noqa: BLE001 — never take the runner down
        logger.warning("refresh failed", exc_info=True)
    finally:
        with _lock:
            _running = False


def prepare(*, run: Runner = subprocess.run, home: Path | None = None) -> dict:
    """Update the marketplaces, this account's plugins and the canopy CLI. Returns
    the health check saying what it did."""
    from . import cli_refresh
    from .desktop import _cli_env, claude_cli

    claude = claude_cli()
    if claude is None:
        return {"name": "refresh", "status": "warn",
                "detail": "no Claude Code CLI here, so plugins were not updated; canopy CLI "
                          + cli_refresh.check(home=home, run=run)}
    env = _cli_env(claude)

    def call(*args: str) -> subprocess.CompletedProcess | None:
        try:
            return run([str(claude), "plugin", *args], capture_output=True, text=True,
                       timeout=TIMEOUT, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("refresh: claude plugin %s: %s", " ".join(args), exc)
            return None

    call("marketplace", "update")
    listed = call("list", "--json")
    try:
        plugins = json.loads(listed.stdout) if listed is not None and listed.returncode == 0 else []
    except ValueError:
        plugins = []
    # This account's installs only: a worktree's local-scope copy is that worktree's.
    ids = sorted({p["id"] for p in plugins if isinstance(p, dict) and p.get("scope") == "user"
                  and p.get("id")})
    failed = []
    for pid in ids:
        out = call("update", pid, "--scope", "user")
        if out is None or out.returncode != 0:
            failed.append(pid)
            if out is not None:
                logger.warning("refresh: %s did not update: %s", pid, (out.stderr or out.stdout)[-300:])
    cli = cli_refresh.check(home=home, run=run)
    detail = f"{len(ids) - len(failed)} of {len(ids)} plugins updated; canopy CLI {cli}"
    if failed:
        detail += f"; did not update: {', '.join(failed)}"
    return {"name": "refresh", "status": "warn" if failed else "ok", "detail": detail}
