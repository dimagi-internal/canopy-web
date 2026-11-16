"""Carry a phone's meeting, end-to-end encrypted, between canopy-web and canopy-listen.

docs/architecture/listen-relay.md. A phone records; canopy-web relays its frames over
/ws/listen/<sid>/; this pipe copies them, as they are, to canopy-listen's local socket
(~/.canopy-listen/serve.sock) and back. It never holds a key, so this runner, like the
server, can't read what was said: a frame is opaque bytes here.

  * ONE PIPE PER SESSION, started from the control channel's `listen_start` or, when that
    socket is down, from the pending-sessions poll.
  * A MEETING OUTLIVES ITS SOCKET: a dropped socket to canopy-web is reopened while
    canopy-listen's side stays open. The server ending the session, or canopy-listen
    closing, closes both, so neither end is left streaming into nothing.
  * NOTHING IS LOGGED BUT IDS AND COUNTS.
"""
from __future__ import annotations

import contextlib
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
    """canopy-listen serve's socket: the same rule as its `store.socket_path`, which this
    package can't import. A socket path must be under 104 bytes on macOS, so a home that
    deep gets a short stand-in named after it. tests/test_listen_pipe.py pins the two."""
    home = Path(os.environ.get("CANOPY_LISTEN_HOME") or Path.home() / ".canopy-listen")
    path = home / "serve.sock"
    if len(str(path).encode()) < 100:
        return path
    import hashlib
    import tempfile

    tag = hashlib.sha256(str(home).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"canopy-listen-{tag}.sock"


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


#: How long a meeting's pipe keeps trying to reach canopy-web again after its socket
#: drops, under the server's PEER_GONE (3 min), after which the session is over anyway.
RECONNECT_FOR = 170.0
#: Close codes that mean "don't come back": signed out, not yours, taken over by a newer
#: socket for this role.
FINAL_CODES = {4001, 4003, 4009}
#: Liveness, as in wake.py: ping when idle, give up on a socket silent this long.
RECV_TIMEOUT = 30
DEAD_AFTER = 2 * RECV_TIMEOUT + 15


class _GoneError(Exception):
    """No socket to canopy-web came back in time."""


class _Link:
    """The current socket to canopy-web, which a meeting's pipe may replace mid-session.
    Frames from canopy-listen wait (bounded) for it rather than being dropped."""

    def __init__(self, ws):
        self.ws = ws
        self.up = threading.Event()
        self.up.set()
        self.over = threading.Event()

    def send(self, frame: bytes, wait: float) -> None:
        while True:
            if self.over.is_set() or not self.up.wait(wait):
                raise _GoneError
            ws = self.ws
            try:
                ws.send_binary(frame)
                return
            except Exception:  # noqa: BLE001 — the socket dropped: wait for the next, resend
                if self.ws is ws:
                    self.up.clear()

    def replace(self, ws) -> None:
        self.ws = ws
        self.up.set()


def _close_code(data) -> int | None:
    if isinstance(data, (bytes, bytearray)) and len(data) >= 2:
        return int.from_bytes(data[:2], "big")
    return None


def _from_server(ws, local, counts: dict, done: threading.Event, clock=time.monotonic) -> tuple[str, str]:
    """Copy one socket's frames to canopy-listen until it ends. Returns (how, reason):
    ("ended", why) the server ended the session, ("final", code) don't reconnect,
    ("dropped", "") the socket went away, ("done", "") the pipe is closing."""
    import websocket

    heard = clock()
    while not done.is_set():
        try:
            opcode, data = ws.recv_data(control_frame=True)
        except websocket.WebSocketTimeoutException:
            if clock() - heard > DEAD_AFTER:
                return "dropped", ""
            try:
                ws.ping()
            except Exception:  # noqa: BLE001
                return "dropped", ""
            continue
        except Exception:  # noqa: BLE001 — closed under us, or a network error
            return ("done", "") if done.is_set() else ("dropped", "")
        heard = clock()
        if opcode == websocket.ABNF.OPCODE_BINARY:
            write_frame(local, data)
            counts["up"] += 1
        elif opcode == websocket.ABNF.OPCODE_TEXT:
            try:
                note = json.loads(data)
            except ValueError:
                continue
            if isinstance(note, dict) and note.get("type") == "ended":
                return "ended", str(note.get("reason") or "ended")[:40]
        elif opcode == websocket.ABNF.OPCODE_CLOSE:
            code = _close_code(data)
            return ("final", f"closed_{code}") if code in FINAL_CODES else ("dropped", "")
    return "done", ""


def pipe(session_id: str, kind: str, hello: dict, *, base_url: str, bearer, connect_ws=None,
         path: Path | None = None, ensure=ensure_serve, sleep=time.sleep, clock=time.monotonic,
         reconnect_for: float = RECONNECT_FOR) -> str:
    """Run one session's pipe to the end. Returns why it ended (a short code).

    A meeting outlives its socket to canopy-web: when that drops (a network change, a
    canopy-web deploy), the pipe opens a new one, for up to `reconnect_for` seconds,
    while canopy-listen's side stays open, so the recording carries on. When the server
    ends the session, canopy-listen is told with an empty frame, the only explicit end;
    any other close leaves canopy-listen holding the meeting for the runner to rejoin."""
    path = path or sock_path()
    if not ensure(path):
        return "listen_not_running"
    if connect_ws is None:
        import websocket

        def connect_ws(url):
            return websocket.create_connection(url, header=[f"Authorization: Bearer {bearer()}"],
                                               timeout=RECV_TIMEOUT)
    url = ws_url(base_url, session_id)
    local = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        local.connect(str(path))
        local.sendall((json.dumps({"op": "session", "session_id": session_id, "kind": kind,
                                   "hello": hello}) + "\n").encode())
        ws = connect_ws(url)
    except Exception as exc:  # noqa: BLE001 — a code, never content
        logger.warning("listen %s: could not open the pipe (%s)", session_id, type(exc).__name__)
        local.close()
        return "pipe_failed"
    link = _Link(ws)
    done = threading.Event()
    counts = {"up": 0, "down": 0, "reconnects": 0}
    reason = {"why": "closed"}
    can_reconnect = kind == "meeting"

    def from_listen():
        try:
            while not done.is_set():
                frame = read_frame(local)
                if frame is None:
                    if not done.is_set():  # the first side to end names the reason
                        reason["why"] = "listen_closed"
                    return
                link.send(frame, reconnect_for if can_reconnect else 0.1)
                counts["down"] += 1
        except _GoneError:
            if not done.is_set():
                reason["why"] = "server_lost"
        except Exception:  # noqa: BLE001
            return
        finally:
            done.set()
            link.over.set()
            with contextlib.suppress(Exception):
                link.ws.close()  # wakes the read below, so both sides close now

    threading.Thread(target=from_listen, daemon=True).start()
    while not done.is_set():
        how, why = _from_server(link.ws, local, counts, done)
        if how == "ended":
            reason["why"] = why
            with contextlib.suppress(Exception):
                write_frame(local, b"")  # the explicit end: canopy-listen finishes the meeting
            break
        if how in ("final", "done") or not can_reconnect:
            if how == "final":
                reason["why"] = why
            break
        link.up.clear()
        with contextlib.suppress(Exception):
            link.ws.close()
        fresh = _reconnect(connect_ws, url, done, sleep=sleep, clock=clock, within=reconnect_for)
        if fresh is None:
            if not done.is_set():
                reason["why"] = "server_lost"
            break
        counts["reconnects"] += 1
        logger.info("listen %s: reconnected to canopy-web", session_id)
        link.replace(fresh)
    done.set()
    link.over.set()
    if reason["why"] in ("listen_closed", "server_lost", "closed"):
        # Why this side left, for the server's log: a code, never content.
        with contextlib.suppress(Exception):
            link.ws.send(json.dumps({"type": "bye", "reason": reason["why"]}))
    for closer in (link.ws.close, lambda: local.shutdown(socket.SHUT_RDWR), local.close):
        with contextlib.suppress(Exception):
            closer()
    logger.info("listen %s (%s) ended: %s; %d frames in, %d out, %d reconnects", session_id, kind,
                reason["why"], counts["up"], counts["down"], counts["reconnects"])
    return reason["why"]


def _reconnect(connect_ws, url: str, done: threading.Event, *, sleep, clock, within: float):
    deadline, delay = clock() + within, 1.0
    while not done.is_set() and clock() < deadline:
        try:
            return connect_ws(url)
        except Exception:  # noqa: BLE001 — offline still; try again
            sleep(delay)
            delay = min(delay * 2, 10.0)
    return None


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
    """Start a pipe for each session this runner should be on: waiting, or a live meeting to rejoin."""
    try:
        status, body = client._call_api("/runner/listen-sessions/pending", method="GET", retry=False)
    except Exception:  # noqa: BLE001 — a server without the route, or offline
        return 0
    if status != 200 or not isinstance(body, (list, dict)):
        return 0
    rows = body if isinstance(body, list) else body.get("items", [])
    return sum(1 for row in rows if isinstance(row, dict) and start(row, base_url=base_url, bearer=bearer))
