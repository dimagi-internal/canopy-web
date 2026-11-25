"""The vault keeper, laptop half: pass on the vault keys this runner holds.

`canopy vault zk keep` asks canopy-web who is waiting for a vault key this machine holds
(a new member of an agent's vault, a machine a trusted person approved) and wraps the key
to each one it trusts (vault access follows canopy access). Running it here means a laptop that holds a key passes it on without
anyone being asked. The cloud runner does the same from its loop (`maybe_keep_vaults`).

When: canopy-web moves this box's keeper version (`keeper_version` in its `/sync`
answer, `runner_sync.SyncBox.keeper`) in the same transaction as any write that gives
it vault work, so a pass runs when that differs from the version the last finished pass
kept at, and otherwise every RECONCILE seconds. With no version to go by (a server
whose `/sync` does not carry it, or none at all), and after a failed pass, every EVERY
seconds. In the background; fail-open (each failure is one log line, and a later pass
tries again). Opt out: VAULT_KEEP=0.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Callable, Optional

from .device_key import canopy_cli
from .platform_jobs import no_window

logger = logging.getLogger(__name__)

EVERY = 120
#: The reconcile while `/sync` carries a keeper version.
RECONCILE = float(os.environ.get("KEEP_RECONCILE_SECONDS", "900"))
#: The shortest gap between two passes a moved version starts.
MIN_GAP = 15
#: How often the loop asks whether a pass is due.
TICK = 5
TIMEOUT = 120

Runner = Callable[..., subprocess.CompletedProcess]


def once(run: Runner = subprocess.run, last: str = "") -> tuple[str, bool]:
    """One keeper pass: (what it printed, whether it finished). The text is logged when
    it changed, or on a grant."""
    cli = canopy_cli()
    if cli is None:
        return last, False
    try:
        out = run([cli, "vault", "zk", "keep"], capture_output=True, text=True, timeout=TIMEOUT,
                  **no_window())
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("vault keeper: could not run (%s)", e)
        return last, False
    text = ((out.stdout or "") + (out.stderr or "")).strip()
    if text and (text != last or "granted:" in text):
        logger.info("vault keeper: %s", text.replace("\n", " | ")[:1500])
    return text, getattr(out, "returncode", 0) == 0


def due(now: float, *, at: Optional[float], kept: Optional[int], version: Optional[int],
        every: float = EVERY) -> bool:
    """Whether a pass is due. `at`: when the last one started (None: never); `kept`: the
    version the last finished pass started at (None: none finished, or it failed)."""
    if at is None:
        return True
    since = now - at
    if version is None or kept is None:
        return since >= every
    if version != kept:
        return since >= MIN_GAP
    return since >= RECONCILE


def start(*, every: float = EVERY, sleep: Callable[[float], None] = None,
          max_passes: Optional[int] = None, version: Callable[[], Optional[int]] = lambda: None,
          clock: Callable[[], float] = time.monotonic, tick: float = TICK,
          **kw) -> Optional[threading.Thread]:
    """Run the keeper in the background. `version`: this box's keeper version from its
    last `/sync` answer, or None when there is none to go by."""
    if os.environ.get("VAULT_KEEP", "1") == "0":
        return None
    sleep = sleep or time.sleep

    def loop() -> None:
        n, last, at, kept = 0, "", None, None
        while max_passes is None or n < max_passes:
            try:
                v = version()
                now = clock()
                if due(now, at=at, kept=kept, version=v, every=every):
                    n += 1
                    at = now
                    last, ok = once(last=last, **kw)
                    kept = v if ok else None
            except Exception as e:  # noqa: BLE001 — never take the runner down
                logger.warning("vault keeper: %s", e)
            sleep(tick)

    t = threading.Thread(target=loop, daemon=True, name="vault-keeper")
    t.start()
    return t
