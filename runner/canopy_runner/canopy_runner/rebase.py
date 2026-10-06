"""Move this runner off an address canopy has left.

canopy moved from labs.connect.dimagi.com/canopy to canopy.dimagi.com
(2026-10-05). The old address still answers, but only while clients hold it,
and every paired runner's runner.json does. canopy cannot edit a laptop's
config, so the runner does it itself: the heartbeat reply names canopy's
canonical address, and a runner configured with the OLD one moves.

Deliberately narrow, because a runner that follows wherever a server points it
is a runner a misconfigured server can strand:

* only off an address in FORMER_BASES — a runner on any other address (a dev
  server, a future deployment) is never moved;
* only to an https address;
* only after that address answers AS THIS RUNNER — this runner's id is in the
  runner list its own token reads there — so it is the same deployment;
* only while idle, so no turn is cut off by the restart.

Then it rewrites base_url in runner.json and exits; launchd's KeepAlive starts
it again on the new address.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

FORMER_BASES = frozenset({"https://labs.connect.dimagi.com/canopy"})


def target_for(current: str, advertised: str) -> str:
    """The address to move to, or "" to stay."""
    current, advertised = (current or "").rstrip("/"), (advertised or "").rstrip("/")
    if current not in FORMER_BASES or not advertised.startswith("https://") or advertised == current:
        return ""
    return advertised


def answers_as(target: str, token: str, runner_id: str, client_cls) -> bool:
    """Whether `target` is the same canopy: this runner exists there for this token."""
    try:
        _, rows = client_cls(target, token)._call("GET", "/runners/", retry=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rebase: %s did not answer (%s); staying put", target, exc)
        return False
    return any(str((r or {}).get("id")) == str(runner_id) for r in (rows or []))


def rewrite(config_path: str, target: str) -> None:
    """Set base_url in runner.json, atomically, keeping its permissions."""
    path = Path(config_path)
    raw = json.loads(path.read_text())
    raw["base_url"] = target
    tmp = path.with_suffix(".json.rebase")
    tmp.write_text(json.dumps(raw, indent=2) + "\n")
    os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)


def observe(me: dict | None, cfg, *, idle: bool, client_cls) -> bool:
    """Move if the heartbeat says to and it is safe. True = config rewritten;
    the caller exits so launchd restarts the runner on the new address."""
    target = target_for(cfg.base_url, (me or {}).get("canonical_base_url", ""))
    if not target or not idle or not getattr(cfg, "config_path", ""):
        return False
    if not answers_as(target, cfg.token, cfg.runner_id, client_cls):
        return False
    try:
        rewrite(cfg.config_path, target)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rebase: could not rewrite %s (%s); staying put", cfg.config_path, exc)
        return False
    logger.warning("rebase: canopy moved to %s — runner.json updated from %s; restarting",
                   target, cfg.base_url)
    return True
