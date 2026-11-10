"""Carry a phone's meeting, end-to-end encrypted, between canopy-web and canopy-listen.

docs/architecture/listen-relay.md. A phone records; canopy-web relays its frames over
/ws/listen/<sid>/; this pipe copies them, as they are, to canopy-listen's local socket
(~/.canopy-listen/serve.sock) and back. It never holds a key, so this runner, like the
server, can't read what was said: a frame is opaque bytes here.

  * ONE PIPE PER SESSION, started from the control channel's `listen_start` or, when that
    socket is down, from the pending-sessions poll.
  * EITHER SIDE CLOSING CLOSES BOTH, so a phone that hangs up never leaves the laptop
    recording, and a laptop that stops never leaves the phone streaming into nothing.
  * NOTHING IS LOGGED BUT IDS AND COUNTS.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_FRAME = 64 * 1024
SERVE_WAIT_SECONDS = 20.0
_active: set[str] = set()
_lock = threading.Lock()


def sock_path() -> Path:
    home = os.environ.get("CANOPY_LISTEN_HOME")
    return Path(home) / "serve.sock" if home else Path.home() / ".canopy-listen" / "serve.sock"


def listen_cli() -> str | None:
    return shutil.which("canopy-listen") or next(
        (str(p) for p in [Path.home() / ".local/bin/canopy-listen"] if p.exists()), None)


def ws_url(base_url: str, session_id: str) -> str:
    b = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1).rstrip("/")
    return f"{b}/ws/listen/{session_id}/?role=laptop"


def ensure_serve(path: Path, *, wait: float = SERVE_WAIT_SECONDS, sleep=time.sleep) -> bool:
    """canopy-listen's daemon is up (started if not). False if it isn't installed or won't start."""
    if _answers(path):
        return True
    cli = listen_cli()
    if not cli:
        logger.warning("listen: canopy-listen is not installed on this Mac")
        return False
    try:
        subprocess.run([cli, "serve", "--detach"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if _answers(path):
            return True
        sleep(0.25)
    return False


def _answers(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(str(path))
            s.sendall(b'{"op":"ping"}\n')
            return bool(s.recv(64))
    except OSError:
        return False


def _read_exact(s: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def read_frame(s: socket.socket) -> bytes | None:
    head = _read_exact(s, 4)
    if head is None:
        return None
    (n,) = struct.unpack(">I", head)
    if n > MAX_FRAME:
        raise ValueError("frame too large")
    return _read_exact(s, n)


def write_frame(s: socket.socket, data: bytes) -> None:
    s.sendall(struct.pack(">I", len(data)) + data)


def pipe(session_id: str, kind: str, hello: dict, *, base_url: str, bearer, connect_ws=None,
         path: Path | None = None, ensure=ensure_serve) -> str:
    """Run one session's pipe to the end. Returns why it ended (a short code)."""
    path = path or sock_path()
    if not ensure(path):
        return "listen_not_running"
    local = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        local.connect(str(path))
        local.sendall((json.dumps({"op": "session", "session_id": session_id, "kind": kind,
                                   "hello": hello}) + "\n").encode())
        if connect_ws is None:
            import websocket

            def connect_ws(url):
                return websocket.create_connection(url, header=[f"Authorization: Bearer {bearer()}"], timeout=30)
        ws = connect_ws(ws_url(base_url, session_id))
    except Exception as exc:  # noqa: BLE001 — a code, never content
        logger.warning("listen %s: could not open the pipe (%s)", session_id, type(exc).__name__)
        local.close()
        return "pipe_failed"
    done = threading.Event()
    counts = {"up": 0, "down": 0}
    reason = {"why": "closed"}

    def from_server():
        import websocket

        try:
            while not done.is_set():
                try:
                    opcode, data = ws.recv_data()
                except websocket.WebSocketTimeoutException:
                    continue
                if opcode == websocket.ABNF.OPCODE_BINARY:
                    write_frame(local, data)
                    counts["up"] += 1
                elif opcode == websocket.ABNF.OPCODE_TEXT:
                    try:
                        note = json.loads(data)
                    except ValueError:
                        continue
                    if note.get("type") == "ended":
                        reason["why"] = str(note.get("reason") or "ended")[:40]
                        return
                elif opcode == websocket.ABNF.OPCODE_CLOSE:
                    return
        except Exception:  # noqa: BLE001
            return
        finally:
            done.set()

    def from_listen():
        try:
            while not done.is_set():
                frame = read_frame(local)
                if frame is None:
                    if not done.is_set():  # the first side to end names the reason
                        reason["why"] = "listen_closed"
                    return
                ws.send_binary(frame)
                counts["down"] += 1
        except Exception:  # noqa: BLE001
            return
        finally:
            done.set()

    threads = [threading.Thread(target=from_server, daemon=True), threading.Thread(target=from_listen, daemon=True)]
    for t in threads:
        t.start()
    done.wait()
    for closer in (ws.close, lambda: local.shutdown(socket.SHUT_RDWR), local.close):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass
    logger.info("listen %s (%s) ended: %s; %d frames in, %d out", session_id, kind, reason["why"],
                counts["up"], counts["down"])
    return reason["why"]


def start(msg: dict, *, base_url: str, bearer) -> bool:
    """From a `listen_start` control frame or a pending session: one background pipe per session."""
    session_id = str(msg.get("session_id") or "")
    kind = str(msg.get("kind") or "")
    if not session_id or kind not in ("pair", "meeting") or not isinstance(msg.get("hello"), dict):
        return False
    with _lock:
        if session_id in _active:
            return False
        _active.add(session_id)

    def run():
        try:
            pipe(session_id, kind, msg["hello"], base_url=base_url, bearer=bearer)
        finally:
            with _lock:
                _active.discard(session_id)

    threading.Thread(target=run, name=f"listen-{session_id[:8]}", daemon=True).start()
    return True


def poll_pending(client, *, base_url: str, bearer) -> int:
    """The fallback when the control socket is down: start a pipe for each waiting session."""
    try:
        status, body = client._call_api("/runner/listen-sessions/pending", method="GET", retry=False)
    except Exception:  # noqa: BLE001 — a server without the route, or offline
        return 0
    if status != 200 or not isinstance(body, (list, dict)):
        return 0
    rows = body if isinstance(body, list) else body.get("items", [])
    return sum(1 for row in rows if isinstance(row, dict) and start(row, base_url=base_url, bearer=bearer))
