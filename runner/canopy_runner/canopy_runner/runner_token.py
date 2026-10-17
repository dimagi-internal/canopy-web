"""This runner signs in as itself — with its device key, not its owner's PAT.

canopy-web hands a runner an hour-long token for a fresh proof of its APPROVED
device key (`POST /api/runner-sessions/`, canopy-web#1594); the token opens the
runner protocol only. The proof is signed by this machine's key — the Secure
Enclave on a Mac, the TPM on Windows — which canopy holds, so the exchange is
canopy's `canopy vault runner-session`, run here the way `device_key.enroll` runs
`canopy vault device --enroll`.

Why: a runner on its owner's PAT is stranded the moment that PAT is revoked — on
2026-10-17 one was, in a token cleanup, and a box silently lost every turn it
claimed. The PAT (`cfg.token`) remains only as the fallback while no approved key
can get a token: a key still pending, an older canopy without the command, no
canopy CLI at all. Opt out entirely with CANOPY_DEVICE_KEY=0.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import threading
import time
from typing import Callable, Optional

from . import device_key

logger = logging.getLogger(__name__)

#: Get the next token this long before the current one runs out.
RENEW_SECONDS = 600
#: After a failed exchange, wait this long before asking again (the PAT is used
#: meanwhile) — a pending key must not cost a subprocess on every call.
RETRY_SECONDS = 300
TIMEOUT = 60
PREFIX = "crt_"


class RunnerCredential:
    """The bearer this runner sends: its own runner token while its device key can
    buy one, else the owner's PAT. Thread-safe; the poll loop, the chat pump and
    the wake listener all share one."""

    def __init__(self, base_url: str, pat: str, runner_id: str, *,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 cli: Optional[str] = None, clock: Callable[[], float] = time.time):
        self._base_url = base_url.rstrip("/")
        self._pat = pat
        self._runner_id = runner_id
        self._run = runner
        self._cli = cli
        self._clock = clock
        self._lock = threading.Lock()
        self._raw = ""
        self._exp = 0.0
        self._failed_at = -RETRY_SECONDS
        self._mode = ""  # "keyless" | "pat" — logged only when it changes

    @classmethod
    def for_config(cls, cfg, **kw) -> "RunnerCredential":
        return cls(cfg.base_url, cfg.token, cfg.runner_id, **kw)

    @property
    def pat(self) -> str:
        return self._pat

    def bearer(self) -> str:
        if not self._runner_id or os.environ.get("CANOPY_DEVICE_KEY", "1") == "0":
            return self._pat
        with self._lock:
            now = self._clock()
            if self._raw and self._exp - now > RENEW_SECONDS:
                return self._raw
            if now - self._failed_at >= RETRY_SECONDS:
                if self._exchange():
                    self._note("keyless", "signed in with this machine's device key (no PAT in use)")
                    return self._raw
                self._failed_at = now
            if self._raw and self._exp > now:
                return self._raw  # still good while the renewal is retried
            self._note("pat", "device key could not get a runner token — using the owner's PAT")
            return self._pat

    def drop(self) -> bool:
        """Forget the runner token after a 401 (its key was revoked mid-hour).
        True if there was one, so the caller retries once with a fresh bearer."""
        with self._lock:
            had = bool(self._raw)
            self._raw, self._exp, self._failed_at = "", 0.0, -RETRY_SECONDS
        return had

    def _note(self, mode: str, msg: str) -> None:
        if mode != self._mode:
            self._mode = mode
            logger.info("runner credential: %s", msg)

    def _exchange(self) -> bool:
        cli = self._cli or device_key.canopy_cli()
        if not cli:
            return False
        try:
            p = self._run([cli, "vault", "runner-session", "--runner", self._runner_id,
                           "--base-url", self._base_url],
                          capture_output=True, text=True, timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — never a new failure; the PAT still works
            logger.debug("canopy vault runner-session failed to run: %s", exc)
            return False
        if p.returncode != 0:
            logger.debug("canopy vault runner-session: %s", (p.stderr or p.stdout or "").strip()[:300])
            return False
        try:
            body = json.loads(p.stdout)
            raw = str(body["access_token"])
            exp = dt.datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00")).timestamp()
        except (ValueError, KeyError, TypeError):
            return False
        if not raw.startswith(PREFIX):
            return False
        self._raw, self._exp = raw, exp
        return True


def bearer_of(token) -> str:
    """A plain token string, or a RunnerCredential's current bearer."""
    return token.bearer() if isinstance(token, RunnerCredential) else token
