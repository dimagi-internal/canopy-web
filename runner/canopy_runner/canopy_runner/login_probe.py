"""Re-test a signed-out box's Claude login directly — the laptop `claude.probe.local`.

A laptop runner learns its login is gone from a session transcript (the Stop hook
sees the CLI's "Please run /login" record, `hooks.report_signed_out`). Before this
module it learned the login was BACK the same way: only when some session on the
box happened to end on a real reply (`hooks.note_signed_in`), or when the
not-ready marker aged out (`readiness.MARKER_TTL_SECONDS`). A signed-out box claims
no turns, so no session arrives to prove anything, and every stale session that
ended on the expired-login message pushed the TTL back. The owner signed in again
and the runner stayed red for up to 15+ minutes.

So while — and only while — the box is marked signed out, `readiness.compute` asks
the Claude Code CLI one short question, at most once per `PROBE_INTERVAL_SECONDS`.
An answer clears the mark at once; "not logged in" leaves it, without touching the
marker's mtime (the TTL keeps counting from the last REAL failure). A healthy box
never probes and spends nothing. Same prompt and classification as the cloud
runner's `_probe_entry` (runner/ec2/cloud_runner.py).

`claude auth status` alone is not used as the signal: it reads the stored login,
and a login that has hit "Login reached its time limit" is exactly the state it
cannot be trusted to report. A real prompt is the only proof.

The probe runs with `--setting-sources project` in an empty scratch dir (no user
hooks or plugins — so the canopy Stop hook never sees it and cannot re-mark the
box) and `--no-session-persistence` (no transcript, so it is never synced as a
session). The last result lives on disk next to the marker so --drain-one's
one-shot process and the heartbeat's health report see the same thing.
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PROBE_PROMPT = "Reply with OK"
#: At most one probe this often, and only while the box is marked signed out.
PROBE_INTERVAL_SECONDS = 90
PROBE_TIMEOUT_SECONDS = 45
CHECK_NAME = "claude.probe.local"
_STATE = "login-probe.json"


def _state_path(cfg) -> Path:
    base = Path(cfg.state_path).parent if getattr(cfg, "state_path", "") else Path.home() / ".canopy"
    return base / _STATE


def last(cfg) -> dict | None:
    """The latest probe: {status, detail, at}, or None if this box never probed."""
    try:
        doc = json.loads(_state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("status") else None


def _save(cfg, result: dict) -> None:
    try:
        p = _state_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass


def classify(returncode: int, stdout: str, stderr: str) -> tuple[str, str]:
    """(status, detail) for one `claude -p --output-format json` run:
    `ok` it answered, `dead` it needs someone to sign in, `unknown` anything else."""
    from canopy_transcript.signed_out import is_auth_required

    result, is_error = "", True
    try:
        doc = json.loads(stdout or "")
        if isinstance(doc, dict):
            result = str(doc.get("result") or "")
            is_error = bool(doc.get("is_error"))
    except ValueError:
        pass
    text = "\n".join(t for t in (result, stdout or "", stderr or "") if t)
    if is_auth_required(text):
        line = next((ln.strip() for ln in text.splitlines() if is_auth_required(ln)), "not signed in")
        return "dead", line[:200]
    if returncode == 0 and not is_error:
        return "ok", "answered a prompt"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "unknown", (f"exit {returncode}: " + (lines[-1] if lines else "no output"))[:200]


def run_probe(cli: Path) -> tuple[str, str]:
    """Ask the CLI one question. Never raises."""
    from . import desktop

    try:
        with tempfile.TemporaryDirectory(prefix="claude-probe-") as scratch:
            proc = subprocess.run(
                [str(cli), "-p", PROBE_PROMPT, "--output-format", "json", "--max-turns", "1",
                 "--no-session-persistence", "--setting-sources", "project"],
                cwd=scratch, env=desktop._cli_env(cli), stdin=subprocess.DEVNULL,
                capture_output=True, timeout=PROBE_TIMEOUT_SECONDS, **desktop.CLI_TEXT)
    except subprocess.TimeoutExpired:
        return "unknown", f"no answer within {PROBE_TIMEOUT_SECONDS}s"
    except Exception as exc:  # noqa: BLE001 — a probe that cannot run is unknown
        return "unknown", f"the probe could not run ({exc})"[:200]
    return classify(proc.returncode, proc.stdout or "", proc.stderr or "")


def maybe_probe(cfg, *, now: float | None = None) -> dict | None:
    """While this box is marked signed out, re-test the login (rate-limited) and
    clear the mark when Claude answers. None when no probe ran."""
    from . import desktop, readiness

    if not readiness.is_signed_out(cfg):
        return None
    now = time.time() if now is None else now
    prev = last(cfg)
    if prev and now - float(prev.get("at") or 0) < PROBE_INTERVAL_SECONDS:
        return None
    cli = desktop.claude_cli()
    if cli is None:
        return None  # readiness reports the missing CLI on its own
    status, detail = run_probe(cli)
    result = {"status": status, "detail": detail, "at": now}
    _save(cfg, result)
    if status == "ok":
        if readiness.clear_signed_out(cfg):
            logger.info("Claude login probe answered on this box — signed-out mark cleared")
    else:
        # Leave the marker exactly as it is: a probe is not a new failure, so it
        # must not restart the TTL.
        logger.info("Claude login probe: %s (%s) — still not claiming", status, detail)
    return result


def health_check(cfg) -> dict | None:
    """The `claude.probe.local` check for the heartbeat's health — when this box's
    login was last proven (or found gone). None until the first probe."""
    probe = last(cfg)
    if probe is None:
        return None
    when = " · probed " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(probe.get("at") or 0)))
    status = probe.get("status")
    if status == "ok":
        return {"name": CHECK_NAME, "status": "ok", "detail": "answered a prompt" + when}
    if status == "dead":
        return {"name": CHECK_NAME, "status": "fail",
                "detail": "not signed in — sign in with `claude /login` on this machine" + when}
    return {"name": CHECK_NAME, "status": "warn",
            "detail": f"could not check: {probe.get('detail') or 'no answer'}" + when}
