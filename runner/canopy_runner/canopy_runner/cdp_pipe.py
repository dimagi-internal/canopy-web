"""Emdash over a DevTools PIPE instead of an open debug port — the opt-in pipe mode.

WHY. The runner drives emdash through Chrome DevTools, which emdash normally exposes
with `--remote-debugging-port`. That port is unauthenticated: anything on the machine,
under ANY macOS account, can connect to it and read or type into every Claude session
in that emdash. Observed 2026-10-22 (failure investigation 1): an Ada session on the
haldimagi account read the screen of a session in acedimagi's emdash on :9223, which
the account's own file permissions had otherwise walled off. Three accounts share one
laptop, so the account boundary was not a boundary for emdash at all.

`--remote-debugging-pipe` gives DevTools to the PARENT PROCESS ONLY, over fds 3/4,
and opens no port. But the runner talks to emdash from short-lived `node` sidecars
(cdp/emdash_control.mjs, one per command), which cannot be emdash's parent. So
`canopy-runner emdash-pipe` is that parent: it launches emdash on the pipe and serves
the runner a DevTools endpoint on the runner's usual `cdp_port` — but one that answers
only a bearer token kept in `~/.canopy/cdp-pipe.json` (0600). Another account cannot
read that file, so it cannot connect. Everything else in the runner is unchanged: the
sidecar and the health checks read the file and send the token (`auth_headers`).

The broker is a proxy for ONE DevTools connection (the pipe), so it serves one client
at a time, and it has to clean up after each one. With a port, a client disconnecting
closes its DevTools connection and Chrome drops that client's state. The pipe never
closes, so the broker does what Chrome would have done: it turns auto-attach and
target discovery off and detaches every session the client attached. Auto-attach
matters most. Playwright sets `waitForDebuggerOnStart`, and if that outlived its
client every new emdash window would sit paused, waiting for a debugger that left.

Stdlib only, like the runner's core loop: a minimal RFC 6455 server, no dependency.

STATUS (2026-10-22): EXPERIMENTAL, NOT USABLE YET. Parked on this branch, unmerged.
- Proven live (haldimagi, Emdash 1.2.8 / Electron 40): emdash on the pipe opens no
  TCP port; no token gets 401; the real Playwright sidecar connects with the token.
- Unproven: a SECOND client. The first live run broke it (internal ids over 2**31,
  which Chrome rejects with no id, so cleanup never ran and the page stayed attached
  to the old session). Fixed here and covered by unit tests, but not re-run live.
- Unanswered: whether emdash survives the broker dying. If it does not, a broker
  crash kills every Claude session inside it, so this decides whether the design
  is acceptable at all.
- Why testing stopped: the broker has to LAUNCH emdash (only the parent holds the
  pipe), and an emdash launched from an agent session's sandboxed shell hangs at
  startup (jj, 2026-10-22: 0% CPU, no renderer, ignores TERM). The sandbox-off
  launch was refused by the auto-mode check, and the haldimagi account was in use.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import platform
import secrets
import signal
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_PATH = Path.home() / ".canopy" / "cdp-pipe.json"
EMDASH_BINARY = Path("/Applications/Emdash.app/Contents/MacOS/Emdash")
WS_PATH = "/devtools/browser/canopy-pipe"
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# How long a second client waits for the first to finish. Sidecar commands take
# seconds; the hook listener and the claim loop can overlap, and the loser should
# wait rather than fail its read.
CLIENT_WAIT_SECONDS = 60.0

# Ids the broker sends on its own behalf (cleanup) live above every id a client could
# plausibly use, and their answers are never forwarded. DevTools ids are 32-bit: an
# id past 2**31-1 is answered "Message must have integer 'id' property" with no id,
# which silently dropped every cleanup call in the first live run (2026-10-22).
_INTERNAL_ID_BASE = 2_000_000_000

# A client may not close emdash through the broker: closing it kills every Claude
# session inside it, and no runner command needs to.
REFUSED_METHODS = frozenset({"Browser.close", "Browser.crash", "Browser.crashGpuProcess"})


# --- the state file: the token and the port, readable by this account only ----------


def write_state(port: int, token: str, *, path: Path | None = None) -> None:
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"port": port, "token": token, "pid": os.getpid()}, fh)
    os.replace(tmp, path)


def clear_state(*, path: Path | None = None) -> None:
    path = path or STATE_PATH
    try:
        data = json.loads(path.read_text())
        if data.get("pid") == os.getpid():   # never remove a newer broker's file
            path.unlink()
    except (OSError, ValueError):
        pass


def token_for(port: int, *, path: Path | None = None) -> str:
    """The pipe broker's token if one serves `port`, else ''. '' means port mode."""
    path = path or STATE_PATH
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return ""
    if int(data.get("port") or 0) != int(port):
        return ""
    return str(data.get("token") or "")


def auth_headers(port: int, *, path: Path | None = None) -> dict:
    tok = token_for(port, path=path)
    return {"Authorization": f"Bearer {tok}"} if tok else {}


# --- websocket framing (server side: reads masked frames, writes unmasked) ----------


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("client closed")
        buf += chunk
    return buf


def read_message(sock: socket.socket) -> str | None:
    """One whole text message, or None when the client closed. Answers pings."""
    parts: list[bytes] = []
    while True:
        b0, b1 = _recv_exact(sock, 2)
        fin, opcode = b0 & 0x80, b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", _recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
        mask = _recv_exact(sock, 4) if b1 & 0x80 else b""
        payload = _recv_exact(sock, length)
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        if opcode == 0x8:
            return None
        if opcode == 0x9:
            send_frame(sock, payload, opcode=0xA)
            continue
        if opcode == 0xA:
            continue
        parts.append(payload)
        if fin:
            return b"".join(parts).decode("utf-8")


def send_frame(sock: socket.socket, payload: bytes, *, opcode: int = 0x1) -> None:
    n = len(payload)
    if n < 126:
        head = struct.pack(">BB", 0x80 | opcode, n)
    elif n < 1 << 16:
        head = struct.pack(">BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack(">BBQ", 0x80 | opcode, 127, n)
    sock.sendall(head + payload)


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()


def _read_request(sock: socket.socket) -> tuple[str, str, dict]:
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed before a request")
        raw += chunk
        if len(raw) > 65536:
            raise ConnectionError("request too large")
    head = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
    method, target, _ = (head[0].split(" ") + ["", ""])[:3]
    headers = {}
    for line in head[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return method, target, headers


def _http(sock: socket.socket, status: str, body: bytes = b"", ctype: str = "text/plain") -> None:
    sock.sendall(f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n"
                 f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)


# --- the broker ----------------------------------------------------------------------


class Broker:
    """Relays one authenticated client at a time between a TCP endpoint and the pipe.

    `to_browser` / `from_browser` are the pipe's file descriptors: the broker writes
    NUL-terminated JSON to the first and reads it from the second (the DevTools pipe
    framing). Tests hand it a fake browser on plain pipes."""

    def __init__(self, to_browser: int, from_browser: int, token: str, port: int):
        self.to_browser = to_browser
        self.from_browser = from_browser
        self.token = token
        self.port = port
        self._write_lock = threading.Lock()
        self._client_lock = threading.Lock()      # one client at a time
        self._state_lock = threading.Lock()
        self._client: socket.socket | None = None
        self._pending: dict[int, tuple[socket.socket, int]] = {}
        self._internal: dict[int, threading.Event] = {}
        self._replies: dict[int, dict] = {}
        self._sessions: set[str] = set()
        self._next_id = 1
        self._next_internal = _INTERNAL_ID_BASE
        self.browser_closed = threading.Event()

    # -- pipe side

    def _send_browser(self, msg: dict) -> None:
        data = json.dumps(msg).encode() + b"\0"
        with self._write_lock:
            view = memoryview(data)
            while view:
                n = os.write(self.to_browser, view)
                view = view[n:]

    def call_browser(self, method: str, params: dict | None = None, timeout: float = 5.0) -> dict:
        """A request the broker makes for itself; its answer is never forwarded."""
        with self._state_lock:
            mid = self._next_internal
            self._next_internal += 1
            done = threading.Event()
            self._internal[mid] = done
        self._send_browser({"id": mid, "method": method, "params": params or {}})
        done.wait(timeout)
        with self._state_lock:
            self._internal.pop(mid, None)
            return self._replies.pop(mid, {})

    def pump_browser(self) -> None:
        """Read the pipe forever; route answers to their caller, events to the client."""
        buf = b""
        while True:
            try:
                chunk = os.read(self.from_browser, 1 << 16)
            except OSError:
                chunk = b""
            if not chunk:
                self.browser_closed.set()
                return
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                try:
                    self._route(json.loads(raw))
                except ValueError:
                    logger.debug("unparsable message from the pipe")

    def _route(self, msg: dict) -> None:
        method = msg.get("method")
        if method == "Target.attachedToTarget":
            sid = (msg.get("params") or {}).get("sessionId")
            if sid:
                with self._state_lock:
                    self._sessions.add(sid)
        elif method == "Target.detachedFromTarget":
            with self._state_lock:
                self._sessions.discard((msg.get("params") or {}).get("sessionId"))
        if "id" in msg:
            mid = msg["id"]
            with self._state_lock:
                if mid in self._internal:
                    self._replies[mid] = msg
                    self._internal[mid].set()
                    return
                owner = self._pending.pop(mid, None)
            if owner is None:
                return            # the client that asked has gone; drop its answer
            sock, client_id = owner
            sid = (msg.get("result") or {}).get("sessionId")
            if sid:
                with self._state_lock:
                    self._sessions.add(sid)
            msg["id"] = client_id
            self._to_client(sock, msg)
            return
        with self._state_lock:
            sock = self._client
        if sock is not None:
            self._to_client(sock, msg)

    def _to_client(self, sock: socket.socket, msg: dict) -> None:
        try:
            send_frame(sock, json.dumps(msg).encode())
        except OSError:
            pass                  # the client's reader notices and cleans up

    # -- client side

    def authorized(self, headers: dict) -> bool:
        got = headers.get("authorization", "")
        return hmac.compare_digest(got.encode(), f"Bearer {self.token}".encode())

    def handle(self, sock: socket.socket) -> None:
        try:
            method, target, headers = _read_request(sock)
        except (ConnectionError, OSError):
            sock.close()
            return
        if not self.authorized(headers):
            _http(sock, "401 Unauthorized", b"canopy pipe broker: bearer token required\n")
            sock.close()
            return
        path = target.split("?", 1)[0].rstrip("/")
        if path in ("/json/version", "/json/version/"):
            ver = self.call_browser("Browser.getVersion").get("result") or {}
            body = json.dumps({
                "Browser": ver.get("product", ""), "Protocol-Version": ver.get("protocolVersion", ""),
                "User-Agent": ver.get("userAgent", ""),
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}{WS_PATH}",
            }).encode()
            _http(sock, "200 OK", body, "application/json")
            sock.close()
            return
        if path != WS_PATH or headers.get("upgrade", "").lower() != "websocket":
            _http(sock, "404 Not Found")
            sock.close()
            return
        if not self._client_lock.acquire(timeout=CLIENT_WAIT_SECONDS):
            _http(sock, "503 Service Unavailable", b"another client holds the pipe\n")
            sock.close()
            return
        try:
            sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\nSec-WebSocket-Accept: "
                          f"{accept_key(headers.get('sec-websocket-key', ''))}\r\n\r\n").encode())
            with self._state_lock:
                self._client = sock
            self._relay(sock)
        finally:
            with self._state_lock:
                self._client = None
                self._pending = {k: v for k, v in self._pending.items() if v[0] is not sock}
            self._reset_after_client()
            self._client_lock.release()
            try:
                sock.close()
            except OSError:
                pass

    def _relay(self, sock: socket.socket) -> None:
        while True:
            try:
                text = read_message(sock)
            except (ConnectionError, OSError, UnicodeDecodeError):
                return
            if text is None:
                return
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            client_id = msg.get("id")
            if msg.get("method") in REFUSED_METHODS:
                self._to_client(sock, {"id": client_id, "error": {
                    "code": -32000, "message": "refused by the canopy pipe broker: "
                    "closing emdash would kill every session in it"}})
                continue
            with self._state_lock:
                mid = self._next_id
                self._next_id += 1
                self._pending[mid] = (sock, client_id)
            msg["id"] = mid
            self._send_browser(msg)

    def _reset_after_client(self) -> None:
        """Undo what the client left on the shared DevTools connection (see module doc)."""
        self.call_browser("Target.setAutoAttach",
                          {"autoAttach": False, "waitForDebuggerOnStart": False, "flatten": True})
        self.call_browser("Target.setDiscoverTargets", {"discover": False})
        with self._state_lock:
            sessions, self._sessions = list(self._sessions), set()
        for sid in sessions:
            self.call_browser("Target.detachFromTarget", {"sessionId": sid})

    def serve(self, server: socket.socket) -> None:
        threading.Thread(target=self.pump_browser, daemon=True, name="pipe-reader").start()
        server.settimeout(1.0)
        while not self.browser_closed.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True,
                             name="pipe-client").start()


# --- launching emdash on the pipe ---------------------------------------------------


def listen(port: int) -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))   # loopback only, exactly like the port it replaces
    server.listen(16)
    return server


def spawn_emdash(binary: Path, extra_args: list[str] | None = None,
                 env: dict | None = None) -> tuple[subprocess.Popen, int, int]:
    """Start emdash with DevTools on fds 3 (it reads) and 4 (it writes).

    Executes the binary directly: `open -a` would make launchd the parent, and the
    pipe belongs to the parent."""
    r_in, w_in = os.pipe()
    r_out, w_out = os.pipe()

    def wire():
        # dup2 onto 3/4 inside the child; the originals may themselves be 3 or 4,
        # so move them out of the way first.
        a, b = os.dup(r_in), os.dup(w_out)
        os.dup2(a, 3)
        os.dup2(b, 4)

    proc = subprocess.Popen([str(binary), "--remote-debugging-pipe", *(extra_args or [])],
                            pass_fds=(3, 4), preexec_fn=wire, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    os.close(r_in)
    os.close(w_out)
    return proc, w_in, r_out


def run(port: int, *, binary: Path = EMDASH_BINARY, state_path: Path | None = None,
        extra_args: list[str] | None = None, env: dict | None = None) -> int:
    """`canopy-runner emdash-pipe`: launch emdash on the pipe and broker it until it exits."""
    if platform.system() == "Windows":
        print("emdash-pipe: pipe mode is POSIX-only (fds 3/4); use the debug port on Windows")
        return 2
    if not binary.exists():
        print(f"emdash-pipe: emdash not found at {binary}")
        return 1
    try:
        server = listen(port)
    except OSError as exc:
        print(f"emdash-pipe: 127.0.0.1:{port} is taken ({exc}). Quit emdash first: one "
              f"launched with --remote-debugging-port is still holding it.")
        return 1
    state_path = state_path or STATE_PATH
    token = secrets.token_urlsafe(32)
    proc, to_browser, from_browser = spawn_emdash(binary, extra_args, env)
    write_state(port, token, path=state_path)
    print(f"emdash-pipe: emdash pid {proc.pid} on the DevTools pipe; runner endpoint "
          f"127.0.0.1:{port} (token in {state_path})")
    broker = Broker(to_browser, from_browser, token, port)

    def _stop(*_):
        raise KeyboardInterrupt

    # A plain `kill` must still remove the token file, exactly like Ctrl-C.
    signal.signal(signal.SIGTERM, _stop)
    try:
        broker.serve(server)
    except KeyboardInterrupt:
        pass
    finally:
        clear_state(path=state_path)
        server.close()
    if proc.poll() is None:
        # The broker is going away and the pipe with it; emdash without its pipe is
        # undrivable, so say so rather than leave a silent half-runner.
        print("emdash-pipe: broker stopped while emdash still runs — the runner sees CDP "
              "down until emdash is relaunched through emdash-pipe")
        return 1
    return 0


def wait_ready(port: int, *, timeout: float = 30.0, state_path: Path | None = None) -> bool:
    """True once the broker answers an authenticated /json/version (for the launcher)."""
    import urllib.request

    end = time.time() + timeout
    while time.time() < end:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/version",
                                     headers=auth_headers(port, path=state_path))
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False
