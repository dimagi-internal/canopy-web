"""Enroll this machine's device key at runner start (Canopy Vault, dimagi-internal/canopy#946).

canopy-web can hand an agent's credentials only to an APPROVED device
(`Agent.require_device_binding`, canopy-web#1538). The key and its enrollment live in
canopy — `canopy vault device --enroll` makes the key (the Secure Enclave via
`~/.canopy/bin/canopy-device-key`, built by install-runner.sh, on a Mac; the TPM through
the "Microsoft Platform Crypto Provider" on Windows, canopy-web#1549; a software key where
there is neither), registers it for this runner (`POST /api/runner-device-keys/`, idempotent
on the key's thumbprint) and caches the status beside it. This module only runs that, once
per start, in the background, as this runner's identity — and says what a person must do:
while the key is `pending` it logs the approve URL and, on macOS, shows ONE notification
per key. `canopy vault status` / `canopy vault device` show the same URL.

The same enrollment registers this machine's ECDH key (``ecdh_public_jwk``) for the
zero-knowledge vault (canopy-web#1550) — canopy makes it beside the signing key. Adding or
changing it sends an active key back to ``pending``, so the notification marker is keyed by
both thumbprints and a re-approval is announced like a first one.

Fail-open everywhere: no canopy CLI, an older canopy without `vault`, an older canopy-web
without the route, no network — each is one log line, and the runner carries on exactly as
before. A runner never blocks or exits over its device key. Opt out: CANOPY_DEVICE_KEY=0.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

TIMEOUT = 120

Runner = Callable[..., subprocess.CompletedProcess]


def canopy_cli(home: Optional[Path] = None) -> Optional[str]:
    """The canopy CLI: on PATH, else where `uv tool install` puts it (launchd's PATH is
    short)."""
    found = shutil.which("canopy")
    if found:
        return found
    home = home or Path.home()
    for cand in (home / ".local" / "bin" / "canopy", home / ".local" / "bin" / "canopy.exe",
                 Path("/opt/homebrew/bin/canopy"), Path("/usr/local/bin/canopy")):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _child_env(cfg, bearer: str = "") -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CANOPY_AGENT", "CANOPY_AGENT_SLUG", "CANOPY_TURN_ID",
                        "CANOPY_CALLER", "OP_SERVICE_ACCOUNT_TOKEN")}
    # Register as THIS runner's identity: its own runner token once it has one
    # (runner_token.RunnerCredential), else its owner's PAT — the only way a box
    # with no approved key yet can register the key in the first place.
    env["CANOPY_WEB_PAT"] = bearer or cfg.token
    env["CANOPY_WEB_API_URL"] = cfg.base_url.rstrip("/")
    return env


def notified_path(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / ".canopy" / "device-key" / ".notified"


def _notify(title: str, text: str, runner: Runner) -> None:
    if sys.platform != "darwin":
        return
    script = (f"display notification {json.dumps(text)} with title {json.dumps(title)}")
    try:
        runner(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — a missed notification is not a failure
        pass


def enroll(cfg, *, runner: Runner = subprocess.run, home: Optional[Path] = None,
           cli: Optional[str] = None, bearer: str = "") -> dict:
    """Run `canopy vault device --enroll` for this runner; returns its status (or
    {"error": …}). Logs the approve URL while pending; notifies once per key."""
    if os.environ.get("CANOPY_DEVICE_KEY", "1") == "0":
        return {"skipped": "CANOPY_DEVICE_KEY=0"}
    cli = cli or canopy_cli(home)
    if not cli:
        # A runner installed from the downloadable installer has no canopy CLI: its key
        # was made and registered by `canopy-runner pair --browser` (device_proof).
        logger.info("device key: no canopy CLI on this machine — not enrolled")
        return {"error": "no canopy CLI"}
    if not (bearer or cfg.token):
        # Never register as anyone: with no runner token yet and no PAT there is no
        # identity to send, and an empty CANOPY_WEB_PAT would only earn a 401.
        logger.info("device key: no runner token and no PAT yet — not enrolling now")
        return {"error": "no credential"}
    try:
        r = runner([cli, "vault", "device", "--enroll", "--runner", cfg.runner_id, "--json"],
                   capture_output=True, text=True, timeout=TIMEOUT, env=_child_env(cfg, bearer),
                   cwd=str(home or Path.home()))
    except Exception as e:  # noqa: BLE001
        logger.warning("device key: enrollment did not run (%s)", e)
        return {"error": str(e)}
    if r.returncode != 0:
        why = " ".join((r.stderr or r.stdout or "").split())[-300:]
        if "No such command" in why:
            why = "this canopy predates `canopy vault` — update canopy"
        logger.warning("device key: not enrolled — %s", why or f"exit {r.returncode}")
        return {"error": why}
    try:
        st = json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        logger.warning("device key: canopy printed no status")
        return {"error": "no status"}
    status, url, jkt = st.get("status"), st.get("approve_url") or "", st.get("jkt") or ""
    ecdh_jkt = st.get("ecdh_jkt") or ""
    logger.info("device key: %s key %s… for runner %s — %s", st.get("kind"), jkt[:12],
                cfg.runner_id, status)
    # The zero-knowledge vault's ECDH key (canopy-web#1550) rides the same enrollment.
    if ecdh_jkt:
        logger.info("device key: ECDH key %s… (%s) registered beside it", ecdh_jkt[:12],
                    st.get("ecdh_kind") or "?")
    elif st.get("ecdh_note"):
        logger.info("device key: no ECDH key — %s", st["ecdh_note"])
    if status == "pending" and url:
        logger.warning("device key: PENDING — approve it in canopy-web (passkey step-up): %s",
                       url)
        marker = notified_path(home)
        try:
            seen = marker.read_text(encoding="utf-8").strip()
        except OSError:
            seen = ""
        # Keyed by BOTH thumbprints: adding/changing the ECDH key on an active key sends it
        # back to pending, and that re-approval deserves its own notification.
        mark = f"{jkt}:{ecdh_jkt}" if ecdh_jkt else jkt
        if seen != mark:
            _notify("Canopy: approve this Mac's device key",
                    "Open canopy-web to approve it (Touch ID). `canopy vault device` shows the link.",
                    runner)
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(mark, encoding="utf-8")
            except OSError:
                pass
    return st


def start(cfg, **kw) -> threading.Thread:
    """Enroll in the background so a slow canopy-web never delays the first claim."""
    def run() -> None:
        try:
            enroll(cfg, **kw)
        except Exception as e:  # noqa: BLE001 — never take the runner down
            logger.warning("device key: enrollment failed (%s)", e)

    t = threading.Thread(target=run, daemon=True, name="device-key-enroll")
    t.start()
    return t
