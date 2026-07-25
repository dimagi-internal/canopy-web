#!/usr/bin/env python3
"""Canopy cloud runner — a headless `kind=cloud` executor for EC2.

Self-contained (stdlib only) on purpose: the emdash-coupled packages/canopy_runner
drives a GUI over CDP, which is wrong for a headless box. This pairs a cloud runner,
claims harness Turns, runs `claude -p` (stream-json) on the turn's prompt, streams
the assistant/tool output into the TurnEvent ledger, and finishes the turn.

Config comes from the environment (see deploy/ec2-runner/README.md):
  CANOPY_BASE_URL   e.g. https://labs.connect.dimagi.com/canopy
  CANOPY_TOKEN      a canopy-web Personal Access Token (Bearer)
  RUNNER_NAME       display name (default: this hostname)
  RUNNER_PROJECTS   comma-separated repo names this runner may drive (e.g. canopy-web)
  RUNNER_AGENTS     comma-separated agent slugs this runner may drive (e.g. echo,ada)
  RUNNER_WORKSPACE  optional workspace slug (defaults to the token's default)
  CLAUDE_BIN        path to the claude binary (default: claude)
  WORK_DIR          where claude runs (default: /tmp/canopy-runner-work)
  POLL_SECONDS      idle poll interval (default: 15)
  STATE_FILE        runner-id cache (default: ~/.canopy-cloud-runner.json)
`claude` authenticates from CLAUDE_CODE_OAUTH_TOKEN (a dedicated setup-token from
Secrets Manager, staged into the service env by cloud-init).
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("CANOPY_BASE_URL", "").rstrip("/")
TOKEN = os.environ.get("CANOPY_TOKEN", "")
RUNNER_NAME = os.environ.get("RUNNER_NAME") or f"cloud-{socket.gethostname()}"


def _csv(name: str) -> list[str]:
    return [x.strip() for x in (os.environ.get(name, "") or "").split(",") if x.strip()]


# Capabilities as plain comma-separated env vars — no JSON in the env file, which
# bash `source` and systemd EnvironmentFile both mangle (they strip the quotes).
RUNNER_CAPS: dict[str, list[str]] = {}
if _csv("RUNNER_PROJECTS"):
    RUNNER_CAPS["projects"] = _csv("RUNNER_PROJECTS")
if _csv("RUNNER_AGENTS"):
    RUNNER_CAPS["agents"] = _csv("RUNNER_AGENTS")
RUNNER_WORKSPACE = os.environ.get("RUNNER_WORKSPACE", "")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
WORK_DIR = os.environ.get("WORK_DIR", "/tmp/canopy-runner-work")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))
# App-level heartbeat cadence (keeps the lease + status fresh).
HEARTBEAT_SECONDS = int(os.environ.get("HEARTBEAT_SECONDS", "20"))
# Short recv poll so the WS loop regains control regularly and drives the heartbeat
# on a wall clock. MUST stay below uvicorn's --ws-ping-interval (5s): the server
# pings every 5s and websocket-client auto-pongs and keeps recv() looping, so a
# heartbeat gated on recv() timing out would never fire and the runner would go
# stale while still connected.
WS_POLL_TIMEOUT = float(os.environ.get("WS_POLL_TIMEOUT", "3"))
STATE_FILE = pathlib.Path(os.environ.get("STATE_FILE", str(pathlib.Path.home() / ".canopy-cloud-runner.json")))

_stop = False


def _log(msg: str) -> None:
    print(f"[cloud-runner] {msg}", flush=True)


def _api(method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
    url = f"{BASE_URL}/api/harness{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        _log(f"{method} {path} -> {exc.code}: {raw[:300]!r}")
        return exc.code, None
    except urllib.error.URLError as exc:
        _log(f"{method} {path} -> URLError {exc.reason}")
        return 0, None


def pair_or_load() -> str:
    if STATE_FILE.exists():
        rid = json.loads(STATE_FILE.read_text()).get("runner_id")
        if rid:
            # Confirm it still exists (a heartbeat 404 means it was retired).
            status, _ = _api("POST", f"/runners/{rid}/heartbeat", {"active_turn_ids": []})
            if status == 200:
                _log(f"reusing runner {rid}")
                return rid
    body = {"name": RUNNER_NAME, "kind": "cloud", "capabilities": RUNNER_CAPS}
    if RUNNER_WORKSPACE:
        body["workspace"] = RUNNER_WORKSPACE
    status, payload = _api("POST", "/runners/", body)
    if status != 201 or not payload:
        _log(f"FATAL: could not pair runner (status={status}). Check CANOPY_BASE_URL/CANOPY_TOKEN.")
        sys.exit(1)
    rid = payload["id"]
    STATE_FILE.write_text(json.dumps({"runner_id": rid}))
    _log(f"paired new runner {rid} ({RUNNER_NAME}, caps={RUNNER_CAPS})")
    return rid


def run_claude(prompt: str, turn_id: str, emit) -> tuple[bool, str]:
    """Run `claude -p` on the prompt, streaming stream-json events via `emit`
    (a callable taking a list of event dicts — WS or REST). Returns (ok, final_text)."""
    workdir = pathlib.Path(WORK_DIR) / turn_id[:8]
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--dangerously-skip-permissions",
    ]
    _log(f"exec: claude -p (turn {turn_id[:8]}) in {workdir}")
    # stderr goes to a file, not PIPE: a chatty claude can fill the 64KB pipe buffer
    # while we're only reading stdout, deadlocking the process. A file has no such
    # limit; we tail it for the failure path below.
    stderr_path = workdir / "stderr.log"
    stderr_file = stderr_path.open("w")
    proc = subprocess.Popen(
        cmd, cwd=str(workdir), stdout=subprocess.PIPE, stderr=stderr_file, text=True
    )
    final_text = ""
    ok = True
    batch: list[dict] = []

    def flush():
        nonlocal batch
        if batch:
            emit(batch)
            batch = []

    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type")
        if etype == "assistant":
            for block in (evt.get("message", {}).get("content") or []):
                if block.get("type") == "text" and block.get("text"):
                    batch.append({"kind": "assistant", "payload": {"text": block["text"]}})
                elif block.get("type") == "tool_use":
                    batch.append({"kind": "tool_start", "payload": {"name": block.get("name", "")}})
        elif etype == "user":
            for block in (evt.get("message", {}).get("content") or []):
                if block.get("type") == "tool_result":
                    batch.append({"kind": "tool_end", "payload": {}})
        elif etype == "result":
            final_text = evt.get("result", "") or ""
            ok = not evt.get("is_error", False)
        if len(batch) >= 10:
            flush()
    proc.wait()
    stderr_file.close()
    flush()
    if proc.returncode != 0 and not final_text:
        ok = False
        try:
            tail = stderr_path.read_text(errors="replace")
        except OSError:
            tail = ""
        final_text = tail[-500:]
    return ok, final_text


def _stage_github_token(token: str) -> None:
    """Wire a git credential helper so `git clone` of private agent repos works
    (used by the reconciler in the next milestone; harmless for a trivial turn)."""
    try:
        subprocess.run(["git", "config", "--global", "credential.helper", "store"],
                       check=False, capture_output=True)
        creds = pathlib.Path.home() / ".git-credentials"
        line = f"https://x-access-token:{token}@github.com\n"
        creds.write_text(line)
        creds.chmod(0o600)
    except OSError as exc:
        _log(f"warn: could not stage github token: {exc}")


def fetch_and_stage_credential(runner_id: str) -> bool:
    """A CLOUD runner owns no secrets at boot beyond its PAT — it fetches its
    credential bundle from canopy-web (the per-runner hub) and stages it into the
    environment. Blocks (polling) until the Claude token is set, so the operator can
    provision the runner AFTER it has paired and appeared in the fleet. A laptop
    runner never does this — it uses emdash's ambient auth.
    """
    while not _stop:
        status, cred = _api("GET", f"/runners/{runner_id}/credential")
        if status == 200 and cred and cred.get("claude_token"):
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = cred["claude_token"]
            if cred.get("op_sa_token"):
                os.environ["OP_SERVICE_ACCOUNT_TOKEN"] = cred["op_sa_token"]
            if cred.get("github_token"):
                _stage_github_token(cred["github_token"])
            _log("staged credential bundle from canopy-web (claude"
                 f"{'+op' if cred.get('op_sa_token') else ''}"
                 f"{'+github' if cred.get('github_token') else ''})")
            return True
        _log("waiting for this runner's credential bundle to be set on canopy-web…")
        time.sleep(POLL_SECONDS)
    return False


# ── REST fallback loop (poll) ───────────────────────────────────────────────
def run_over_rest(runner_id: str) -> None:
    _log(f"polling {BASE_URL} every {POLL_SECONDS}s (REST fallback)")
    while not _stop:
        _api("POST", f"/runners/{runner_id}/heartbeat", {"active_turn_ids": []})
        status, turn = _api("POST", f"/runners/{runner_id}/claim")
        if status != 200 or not turn:
            time.sleep(POLL_SECONDS)
            continue
        turn_id = turn["id"]
        _log(f"claimed turn {turn_id[:8]} target={turn.get('target')} (REST)")
        _api("POST", f"/turns/{turn_id}/start", {"session_id": f"cloud-{turn_id[:8]}"})
        _api("POST", f"/runners/{runner_id}/heartbeat", {"active_turn_ids": [turn_id]})

        def emit(events, _tid=turn_id):
            _api("POST", f"/turns/{_tid}/events", {"events": events})

        try:
            ok, text = run_claude(turn.get("prompt", ""), turn_id, emit)
        except Exception as exc:  # never let one turn kill the loop
            ok, text = False, f"runner error: {exc}"
        finish = "done" if ok else "failed"
        _api("POST", f"/turns/{turn_id}/finish", {"status": finish, "result_note": text[:2000]})
        _log(f"finished turn {turn_id[:8]}: {finish}")


# ── WebSocket control channel (RC2) ─────────────────────────────────────────
def _ws_url(runner_id: str) -> str:
    base = BASE_URL.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return f"{base.replace('/api', '')}/ws/runner/{runner_id}/"


def _ws_request(ws, frame: dict, want_type: str, timeout: float = 120.0):
    """Send an action frame and read until the matching ack/result, skipping
    unrelated frames (a wake/interject that arrives mid-request is not what we're
    waiting on right now). Returns the matched frame, or None on close/timeout."""
    import websocket  # local: only the WS path needs the dep

    # ws.settimeout() also governs sends, not just recv. A large event frame (e.g.
    # drill doctor output) can take longer to write than the short WS_POLL_TIMEOUT
    # allows; if the socket times out mid-write, OpenSSL has already handed part of
    # the frame to the kernel and a subsequent SSL_write MUST resend the exact same
    # buffer or it raises ssl.SSLError BAD_LENGTH — killing the process. Give sends
    # a generous timeout, then restore the short poll timeout for the recv loop.
    ws.settimeout(60)
    try:
        ws.send(json.dumps(frame))
    finally:
        ws.settimeout(WS_POLL_TIMEOUT)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue  # socket idle; keep waiting for our ack
        if not raw:
            return None
        msg = json.loads(raw)
        if msg.get("type") == want_type:
            return msg
    return None


def _claim_and_run(ws, runner_id: str) -> None:
    res = _ws_request(ws, {"action": "claim"}, "claim.result")
    turn = res.get("turn") if res else None
    if not turn:
        return
    tid = turn["id"]
    _log(f"claimed turn {tid[:8]} target={turn.get('target')} (WS)")
    _ws_request(ws, {"action": "start", "turn_id": tid, "session_id": f"cloud-{tid[:8]}"}, "start.ack")

    def emit(events, _tid=tid):
        _ws_request(ws, {"action": "event", "turn_id": _tid, "events": events}, "event.ack", timeout=60)

    try:
        ok, text = run_claude(turn.get("prompt", ""), tid, emit)
    except Exception as exc:
        ok, text = False, f"runner error: {exc}"
    _ws_request(ws, {"action": "finish", "turn_id": tid,
                     "status": "done" if ok else "failed", "result_note": text[:2000]}, "finish.ack")
    _log(f"finished turn {tid[:8]} (WS): {'done' if ok else 'failed'}")


def run_over_ws(runner_id: str) -> bool:
    """Persistent control channel: heartbeat, claim-on-wake, run + stream over the
    socket. Returns False if the WS lib/endpoint is unavailable (caller falls back
    to REST); loops until _stop otherwise, reconnecting on drops."""
    try:
        import websocket
    except ImportError:
        _log("websocket-client not installed; using REST")
        return False
    url = _ws_url(runner_id)
    connected_ever = False
    while not _stop:
        try:
            ws = websocket.create_connection(
                url, header=[f"Authorization: Bearer {TOKEN}"], timeout=HEARTBEAT_SECONDS,
            )
        except Exception as exc:
            if not connected_ever:
                _log(f"ws connect failed ({exc}); falling back to REST")
                return False
            _log(f"ws reconnect failed ({exc}); retry in {POLL_SECONDS}s")
            time.sleep(POLL_SECONDS)
            continue
        connected_ever = True
        _log(f"ws connected: {url}")
        # Poll on a short timeout so recv() hands control back regularly; the
        # heartbeat is driven on a wall clock below, NOT gated on recv timing out
        # (see WS_POLL_TIMEOUT — server pings would otherwise starve the heartbeat).
        ws.settimeout(WS_POLL_TIMEOUT)

        def _beat():
            _ws_request(ws, {"action": "heartbeat", "active_turn_ids": []}, "heartbeat.ack", timeout=15)

        try:
            _beat()  # register ONLINE immediately (claim_next_turn gates on a fresh heartbeat)
            last_beat = time.monotonic()
            _claim_and_run(ws, runner_id)  # drain anything already queued (no wake for those)
            while not _stop:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    raw = None
                if raw:
                    msg = json.loads(raw)
                    mtype = msg.get("type")
                    if mtype == "wake":
                        _claim_and_run(ws, runner_id)
                    elif mtype == "interject":
                        _log(f"interject turn={msg.get('turn_id')}: {msg.get('message')!r}")
                elif raw == "":
                    break  # server closed the socket
                if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                    _beat()
                    last_beat = time.monotonic()
        except Exception as exc:
            _log(f"ws loop error: {exc}")
        finally:
            try:
                ws.close()
            except Exception:
                pass
        if not _stop:
            time.sleep(2)  # brief backoff before reconnect
    return True


def main() -> None:
    if not BASE_URL or not TOKEN:
        _log("FATAL: CANOPY_BASE_URL and CANOPY_TOKEN are required")
        sys.exit(1)
    runner_id = pair_or_load()
    if not fetch_and_stage_credential(runner_id):
        return  # stopped before a credential was provisioned
    # Prefer the WS control channel; fall back to REST polling if it can't be used.
    if not run_over_ws(runner_id):
        run_over_rest(runner_id)


def _handle_stop(*_a):
    global _stop
    _stop = True
    _log("stopping after current turn")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    main()
