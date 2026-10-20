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
`~/.canopy/pairing.json` carries the code for the menu-bar app meanwhile.
"""
from __future__ import annotations

import logging
import os
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
                 spawn: Optional[Callable[[Callable[[], None]], None]] = None):
        self.config_path, self.base_url, self.runner_id = Path(config_path), base_url, runner_id
        self.streak = 0
        self.last_attempt: Optional[float] = None
        self.running = False
        self._pair = pair_fn
        self._restart = restart or _reexec
        self._clock = clock
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True,
                                                           name="runner-reconnect").start())

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
                         out=lambda line: logger.info("reconnect: %s", line))
            if rc == 0:
                logger.info("reconnected — restarting signed in with this machine's device key")
                self._restart()
        except Exception as exc:  # noqa: BLE001 — try again after RETRY_SECONDS
            logger.warning("reconnect did not finish: %s", exc)
        finally:
            self.running = False


def _reexec() -> None:
    os.execv(sys.executable, [sys.executable, *sys.argv])
