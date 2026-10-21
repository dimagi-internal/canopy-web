"""Keep this account's `canopy` CLI at the canopy plugin's version.

canopy ships twice: the plugin through Claude Code's marketplace clone
(`~/.claude/plugins/marketplaces/canopy`), and the CLI as a separate `uv tool install`
from that clone. The runner updates itself, and Claude Code pulls the marketplace, but
nothing on a laptop reinstalled the CLI — so a box kept a fresh plugin and an old CLI
until someone ran `canopy update` by hand. jj sat at 0.2.685 while sessions there
fetched credentials through it and got a 401 from the newer server. The cloud runner
already treats a CLI behind the plugin as a fault it refreshes (`canopy.cli`,
runner/ec2/cloud_runner.py); this is the laptop half.

In the background, at start and every CHECK_SECONDS: update the canopy marketplace,
compare `canopy --version` with the plugin manifest, and when the CLI is BEHIND, run the
reinstall canopy's own doctor names (`uv tool install --reinstall --force <clone>`).
Only behind: a CLI ahead of the clone is a developer's install and is left alone. No
CLI at all is left alone too — installing it is setup's job, not a refresh's.

Fail-open everywhere: no uv, no clone, a reinstall that fails (on Windows a running
canopy.exe cannot be replaced) — each is one log line, and the next check tries again.
Opt out: CANOPY_CLI_REFRESH=0.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .device_key import canopy_cli

logger = logging.getLogger(__name__)

CHECK_SECONDS = 6 * 3600
TIMEOUT = 300

Runner = Callable[..., subprocess.CompletedProcess]


def marketplace_clone(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / ".claude" / "plugins" / "marketplaces" / "canopy"


def plugin_version(home: Optional[Path] = None) -> str:
    """The canopy plugin version the marketplace clone holds (read like cloud_runner)."""
    root = marketplace_clone(home)
    for rel in ("plugins/canopy/.claude-plugin/plugin.json", ".claude-plugin/plugin.json"):
        try:
            v = json.loads((root / rel).read_text()).get("version")
        except (OSError, ValueError):
            continue
        if v:
            return str(v)
    return ""


def _parse(v: str) -> tuple:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else ()


def _uv(home: Path) -> Optional[str]:
    found = shutil.which("uv")
    if found:
        return found
    for cand in (home / ".local" / "bin" / "uv", home / ".local" / "bin" / "uv.exe",
                 home / ".cargo" / "bin" / "uv", Path("/opt/homebrew/bin/uv"),
                 Path("/usr/local/bin/uv")):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _env(home: Path) -> dict:
    extra = [str(home / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    return {**os.environ, "PATH": os.pathsep.join(extra + [os.environ.get("PATH", "")])}


def _version_of(cmd: list[str], run: Runner, env: dict) -> str:
    try:
        out = run(cmd, capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "") if out.returncode == 0 else ""


def check(home: Optional[Path] = None, run: Runner = subprocess.run) -> str:
    """One check; returns what it did ("current", "refreshed", or why it skipped)."""
    home = home or Path.home()
    env = _env(home)
    from .desktop import claude_cli

    claude = claude_cli()
    if claude is not None:
        # Claude Code pulls marketplaces on its own schedule; pull now so "behind"
        # is measured against what canopy has actually released.
        try:
            run([str(claude), "plugin", "marketplace", "update", "canopy"], capture_output=True,
                text=True, timeout=120, env=env)
        except (OSError, subprocess.SubprocessError):
            pass
    want = _parse(plugin_version(home))
    if not want:
        return "no canopy marketplace clone"
    cli = canopy_cli(home)
    if cli is None:
        return "no canopy CLI"
    have = _parse(_version_of([cli, "--version"], run, env))
    if have and have >= want:
        return "current"
    uv = _uv(home)
    if uv is None:
        logger.warning("canopy CLI %s is behind the plugin %s, and there is no uv to reinstall it",
                       ".".join(map(str, have)) or "?", ".".join(map(str, want)))
        return "no uv"
    try:
        out = run([uv, "tool", "install", "--reinstall", "--force", str(marketplace_clone(home))],
                  capture_output=True, text=True, timeout=TIMEOUT, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("canopy CLI reinstall failed (%s)", e)
        return "reinstall failed"
    if out.returncode != 0:
        logger.warning("canopy CLI reinstall failed: %s", (out.stderr or out.stdout)[-400:])
        return "reinstall failed"
    logger.info("canopy CLI %s -> %s (reinstalled from the marketplace clone)",
                ".".join(map(str, have)) or "?", ".".join(map(str, want)))
    return "refreshed"


def start(*, every: float = CHECK_SECONDS, sleep: Callable[[float], None] = None,
          max_checks: Optional[int] = None, **kw) -> Optional[threading.Thread]:
    if os.environ.get("CANOPY_CLI_REFRESH", "1") == "0":
        return None
    sleep = sleep or time.sleep

    def loop() -> None:
        n = 0
        while max_checks is None or n < max_checks:
            n += 1
            try:
                check(**kw)
            except Exception as e:  # noqa: BLE001 — never take the runner down
                logger.warning("canopy CLI refresh: %s", e)
            sleep(every)

    t = threading.Thread(target=loop, daemon=True, name="canopy-cli-refresh")
    t.start()
    return t
