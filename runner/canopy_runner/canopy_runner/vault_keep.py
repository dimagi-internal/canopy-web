"""The vault keeper, laptop half: pass on the vault keys this runner holds.

`canopy vault zk keep` asks canopy-web who is waiting for a vault key this machine holds
(a new member of an agent's vault, a machine a trusted person approved) and wraps the key
to each one it trusts (canopy-web spec 2026-10-22-vault-access-follows-canopy-access-
design.md §1). Running it here means a laptop that holds a key passes it on without
anyone being asked. The cloud runner does the same from its loop (`maybe_keep_vaults`).

In the background, every EVERY seconds; fail-open (each failure is one log line, and
the next pass tries again). Opt out: VAULT_KEEP=0.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Callable, Optional

from .device_key import canopy_cli

logger = logging.getLogger(__name__)

EVERY = 120
TIMEOUT = 120

Runner = Callable[..., subprocess.CompletedProcess]


def once(run: Runner = subprocess.run, last: str = "") -> str:
    """One keeper pass; returns what it printed (logged when it changed, or on a grant)."""
    cli = canopy_cli()
    if cli is None:
        return last
    try:
        out = run([cli, "vault", "zk", "keep"], capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("vault keeper: could not run (%s)", e)
        return last
    text = ((out.stdout or "") + (out.stderr or "")).strip()
    if text and (text != last or "granted:" in text):
        logger.info("vault keeper: %s", text.replace("\n", " | ")[:1500])
    return text


def start(*, every: float = EVERY, sleep: Callable[[float], None] = None,
          max_passes: Optional[int] = None, **kw) -> Optional[threading.Thread]:
    if os.environ.get("VAULT_KEEP", "1") == "0":
        return None
    sleep = sleep or time.sleep

    def loop() -> None:
        n, last = 0, ""
        while max_passes is None or n < max_passes:
            n += 1
            try:
                last = once(last=last, **kw)
            except Exception as e:  # noqa: BLE001 — never take the runner down
                logger.warning("vault keeper: %s", e)
            sleep(every)

    t = threading.Thread(target=loop, daemon=True, name="vault-keeper")
    t.start()
    return t
