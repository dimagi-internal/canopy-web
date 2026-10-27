"""A runner whose token canopy-web stopped accepting reconnects itself from the browser.

The old failure (jj-mbp-cdp, 2026-10-15 → 10-20): the PAT in runner.json expired, every
heartbeat got 401, the box never learned it was out of date, and its updater asked with
the same dead token and did nothing — a deadlock only a shell on the box could break.

Now: after a few heartbeats in a row are refused (401) and this box has no active
device key that could sign it in instead, the runner starts a browser pairing that
names itself (`browser_pair.run_browser_pair(reconnect=True)`). It opens the approve
page on this machine; when the runner's owner approves it (passkey), the runner gets
this machine's key back — same runner, same settings — runner.json drops the dead
token, and the runner restarts itself signed in with the key (canopy-web#1705).
`~/.canopy/pairing.json` carries the code for the menu-bar app meanwhile. Windows has
no menu bar showing it, so there a small notice names the code before the browser
opens — once per code (`notify_code`).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: Refused heartbeats in a row before reconnecting (one is a blip; three is a dead token).
DEAD_AFTER = 3
#: At most one reconnect attempt this often (a pairing lives 15 minutes).
RETRY_SECONDS = 30 * 60
#: The Windows notice closes itself with the pairing it names.
NOTICE_SECONDS = 15 * 60

# The Windows notice: WScript.Shell.Popup (dialog.py's renderer), OK-only with an info
# icon (0 + 64), timing out with the pairing. The text arrives on stdin, never
# interpolated, so nothing in it can break into PowerShell.
_NOTICE_PS = r"""
$msg = [Console]::In.ReadToEnd()
try {
  $w = New-Object -ComObject WScript.Shell
  $null = $w.Popup($msg, $env:CANOPY_NOTICE_TIMEOUT -as [int], "Canopy Runner - reconnect", 64)
} catch {}
"""


def notify_code(code: str, url: str, *, popen=subprocess.Popen) -> bool:
    """Show a Windows notice naming the reconnect code, without waiting for it: the
    popup lives in its own windowless PowerShell. False if it could not start."""
    msg = (f"Canopy stopped accepting this runner's sign-in.\n\n"
           f"Approve this computer in your browser - check the code is {code}.\n\n{url}")
    from .platform_jobs import no_window

    try:
        proc = popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", _NOTICE_PS],
                     stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     text=True, env={**os.environ, "CANOPY_NOTICE_TIMEOUT": str(NOTICE_SECONDS)},
                     **no_window())
        proc.stdin.write(msg)
        proc.stdin.close()
    except (OSError, ValueError) as exc:
        logger.info("reconnect: could not show the code notice: %s", exc)
        return False
    return True


def is_auth_refusal(exc: BaseException) -> bool:
    """A heartbeat refused for its credential (not a 5xx, not the network)."""
    text = str(exc)
    return "/heartbeat" in text and "-> 401" in text


def _key_signs_in(runner_id: str) -> bool:
    """This box has an ACTIVE device key registered for this runner — the runner token
    flow signs in with it, so a reconnect would change nothing."""
    try:
        from . import device_proof

        st = device_proof.read_status()
    except Exception:  # noqa: BLE001 — unreadable status = no key to rely on
        return False
    return st.get("status") == "active" and str(st.get("runner") or st.get("runner_id") or "") == runner_id


class AuthWatch:
    def __init__(self, config_path: Path, base_url: str, runner_id: str, *,
                 pair_fn: Optional[Callable[..., int]] = None,
                 restart: Optional[Callable[[], None]] = None,
                 clock: Callable[[], float] = time.time,
                 spawn: Optional[Callable[[Callable[[], None]], None]] = None,
                 notice: Callable[[str, str], object] | None = None,
                 browser: Callable[[str], bool] | None = None):
        self.config_path, self.base_url, self.runner_id = Path(config_path), base_url, runner_id
        self.streak = 0
        self.last_attempt: Optional[float] = None
        self.running = False
        self._pair = pair_fn
        self._restart = restart or _reexec
        self._clock = clock
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True,
                                                           name="runner-reconnect").start())
        self._notice = notice or notify_code
        self._browser = browser
        self._noticed: set[str] = set()

    def note(self, exc: BaseException) -> None:
        self.streak = self.streak + 1 if is_auth_refusal(exc) else 0

    def ok(self) -> None:
        self.streak = 0

    def maybe_reconnect(self) -> bool:
        """Start a reconnect when the token looks dead. True if one was started."""
        if self.streak < DEAD_AFTER or self.running:
            return False
        if self.last_attempt is not None and self._clock() - self.last_attempt < RETRY_SECONDS:
            return False
        if _key_signs_in(self.runner_id):
            return False
        self.running, self.last_attempt = True, self._clock()
        logger.warning("canopy refused this runner's token %d times in a row — opening a browser "
                       "pairing to reconnect it (approve it as the runner's owner)", self.streak)
        self._spawn(self._run)
        return True

    def _run(self) -> None:
        try:
            if self._pair is None:
                from .browser_pair import run_browser_pair as pair_fn
            else:
                pair_fn = self._pair
            rc = pair_fn(self.config_path, base_url=self.base_url, reconnect=True,
                         open_browser=self._open_browser,
                         out=lambda line: logger.info("reconnect: %s", line))
            if rc == 0:
                logger.info("reconnected — restarting signed in with this machine's device key")
                self._restart()
        except Exception as exc:  # noqa: BLE001 — try again after RETRY_SECONDS
            logger.warning("reconnect did not finish: %s", exc)
        finally:
            self.running = False

    def _open_browser(self, url: str) -> bool:
        """On Windows, name the code first (no menu bar shows it there); once per code."""
        from .platform_jobs import is_windows

        code = url.rstrip("/").rsplit("/", 1)[-1]
        if is_windows() and code and code not in self._noticed:
            self._noticed.add(code)
            self._notice(code, url)
        if self._browser is not None:
            return self._browser(url)
        from .browser_pair import _open_browser

        return _open_browser(url)


def _reexec() -> None:
    os.execv(sys.executable, [sys.executable, *sys.argv])
