"""Pipe mode: emdash's DevTools behind a token instead of an open debug port.

2026-10-22 (failure investigation 1): a session on one macOS account read the screen
of a session in another account's emdash through its unauthenticated debug port.
`canopy-runner emdash-pipe` launches emdash on `--remote-debugging-pipe` and serves
the runner's cdp_port behind a token in a 0600 file. These tests run the broker
against a fake browser on plain pipes: the token is required, the runner's commands
round-trip, nobody can close emdash through it, and a client leaves no DevTools state
behind on the shared pipe (above all, no `waitForDebuggerOnStart` auto-attach).
"""
import base64
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest

from canopy_runner import cdp_control, cdp_pipe, pair

CDP = Path(__file__).resolve().parents[1] / "canopy_runner" / "cdp"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


# --- the state file -------------------------------------------------------------------


def test_state_file_is_private_and_names_its_port(tmp_path):
    path = tmp_path / "cdp-pipe.json"
    cdp_pipe.write_state(9224, "tok", path=path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert cdp_pipe.token_for(9224, path=path) == "tok"
    assert cdp_pipe.auth_headers(9224, path=path) == {"Authorization": "Bearer tok"}


def test_no_file_or_another_port_is_port_mode(tmp_path):
    path = tmp_path / "cdp-pipe.json"
    assert cdp_pipe.auth_headers(9222, path=path) == {}
    cdp_pipe.write_state(9224, "tok", path=path)
    assert cdp_pipe.auth_headers(9222, path=path) == {}


def test_clear_state_leaves_a_newer_brokers_file(tmp_path):
    path = tmp_path / "cdp-pipe.json"
    path.write_text(json.dumps({"port": 9224, "token": "theirs", "pid": os.getpid() + 1}))
    cdp_pipe.clear_state(path=path)
    assert path.exists()
    cdp_pipe.write_state(9224, "mine", path=path)
    cdp_pipe.clear_state(path=path)
    assert not path.exists()


# --- the health probes send the token ----------------------------------------------------


def test_cdp_healthy_sends_the_pipe_token(monkeypatch, tmp_path):
    path = tmp_path / "cdp-pipe.json"
    cdp_pipe.write_state(9224, "tok", path=path)
    monkeypatch.setattr(cdp_pipe, "STATE_PATH", path)
    seen = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen["auth"] = req.get_header("Authorization")
        return Resp()

    monkeypatch.setattr(cdp_control.urllib.request, "urlopen", fake_urlopen)
    assert cdp_control.cdp_healthy(port=9224) is True
    assert seen["auth"] == "Bearer tok"


@needs_node
def test_sidecar_reads_the_same_token(tmp_path):
    path = tmp_path / "cdp-pipe.json"
    cdp_pipe.write_state(9224, "tok", path=path)
    script = (
        f"const m = await import({json.dumps((CDP / 'pipe_auth.mjs').as_uri())});"
        f"process.stdout.write(JSON.stringify([m.pipeHeaders(9224, {json.dumps(str(path))}),"
        f" m.pipeHeaders(9222, {json.dumps(str(path))})]));"
    )
    out = subprocess.run(["node", "--input-type=module", "-e", script],
                         capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [{"Authorization": "Bearer tok"}, {}]


def test_sidecar_connects_with_the_pipe_headers():
    src = (CDP / "emdash_control.mjs").read_text()
    assert "{ headers: pipeHeaders(port) }" in src


# --- the broker, against a fake browser -------------------------------------------------


class FakeBrowser:
    """The other end of the pipe: records every request, answers each with its id."""

    def __init__(self):
        self.to_broker_r, self.to_broker_w = os.pipe()     # browser writes, broker reads
        self.from_broker_r, self.from_broker_w = os.pipe()  # broker writes, browser reads
        self.received: list[dict] = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        buf = b""
        while True:
            chunk = os.read(self.from_broker_r, 65536)
            if not chunk:
                return
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                msg = json.loads(raw)
                if not 0 <= msg["id"] < 2 ** 31:
                    # What Chrome does with an id past 32 bits: an error with NO id, so
                    # the request is lost (the first live run's cleanup, 2026-10-22).
                    self.send({"error": {"code": -32600,
                                         "message": "Message must have integer 'id' property"}})
                    continue
                self.received.append(msg)
                result = {}
                if msg["method"] == "Browser.getVersion":
                    result = {"product": "Chrome/1", "protocolVersion": "1.3"}
                if msg["method"] == "Target.attachToTarget":
                    result = {"sessionId": "S1"}
                self.send({"id": msg["id"], "result": result})

    def send(self, msg):
        os.write(self.to_broker_w, json.dumps(msg).encode() + b"\0")

    def methods(self):
        return [m["method"] for m in self.received]


@pytest.fixture
def broker():
    fake = FakeBrowser()
    server = cdp_pipe.listen(0)
    port = server.getsockname()[1]
    b = cdp_pipe.Broker(fake.from_broker_w, fake.to_broker_r, "tok", port)
    threading.Thread(target=b.serve, args=(server,), daemon=True).start()
    yield fake, port
    server.close()


def _http(port, path, headers=None):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n{extra}\r\n".encode())
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return head.split(b"\r\n")[0].decode(), body


class WS:
    """A minimal masked-frame websocket client."""

    def __init__(self, port, token="tok"):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {cdp_pipe.WS_PATH} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                        f"Sec-WebSocket-Version: 13\r\nAuthorization: Bearer {token}\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.s.recv(1)
        self.status = head.split(b"\r\n")[0].decode()
        assert cdp_pipe.accept_key(key) in head.decode() or "101" not in self.status

    def send(self, msg):
        payload = json.dumps(msg).encode()
        mask = os.urandom(4)
        head = struct.pack(">BB", 0x81, 0x80 | 126) + struct.pack(">H", len(payload))
        self.s.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))

    def recv(self):
        return json.loads(cdp_pipe.read_message(self.s))

    def close(self):
        self.s.sendall(struct.pack(">BB", 0x88, 0x80) + os.urandom(4))
        self.s.close()


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_no_token_no_devtools(broker):
    fake, port = broker
    status, _ = _http(port, "/json/version")
    assert "401" in status
    status, _ = _http(port, "/json/version", {"Authorization": "Bearer wrong"})
    assert "401" in status
    assert WS(port, token="wrong").status.endswith("401 Unauthorized")
    assert fake.received == []


def test_version_points_playwright_at_the_broker(broker):
    _, port = broker
    status, body = _http(port, "/json/version/", {"Authorization": "Bearer tok"})
    assert "200" in status
    info = json.loads(body)
    assert info["Browser"] == "Chrome/1"
    assert info["webSocketDebuggerUrl"] == f"ws://127.0.0.1:{port}{cdp_pipe.WS_PATH}"


def test_a_command_round_trips_under_the_clients_own_id(broker):
    fake, port = broker
    ws = WS(port)
    assert "101" in ws.status
    ws.send({"id": 7, "method": "Target.getTargets"})
    reply = ws.recv()
    assert reply["id"] == 7 and "result" in reply
    assert fake.received[-1]["method"] == "Target.getTargets"
    ws.close()


def test_events_reach_the_client(broker):
    fake, port = broker
    ws = WS(port)
    ws.send({"id": 1, "method": "Target.getTargets"})
    ws.recv()
    fake.send({"method": "Target.targetCreated", "params": {"targetInfo": {}}})
    assert ws.recv()["method"] == "Target.targetCreated"
    ws.close()


def test_nobody_closes_emdash_through_the_broker(broker):
    fake, port = broker
    ws = WS(port)
    ws.send({"id": 3, "method": "Browser.close"})
    reply = ws.recv()
    assert reply["id"] == 3 and "refused" in reply["error"]["message"]
    assert "Browser.close" not in fake.methods()
    ws.close()


def test_a_departing_client_leaves_no_devtools_state(broker):
    """Playwright sets waitForDebuggerOnStart; outliving its client would pause
    every new emdash window. The broker undoes it, and detaches what it attached."""
    fake, port = broker
    ws = WS(port)
    ws.send({"id": 1, "method": "Target.setAutoAttach",
             "params": {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}})
    ws.recv()
    ws.send({"id": 2, "method": "Target.attachToTarget", "params": {"targetId": "T"}})
    assert ws.recv()["result"]["sessionId"] == "S1"
    fake.send({"method": "Target.attachedToTarget", "params": {"sessionId": "S2"}})
    ws.recv()
    ws.close()
    assert _wait(lambda: fake.methods().count("Target.detachFromTarget") == 2)
    reset = next(m for m in fake.received[2:] if m["method"] == "Target.setAutoAttach")
    assert reset["params"]["autoAttach"] is False
    assert reset["params"]["waitForDebuggerOnStart"] is False
    assert "Target.setDiscoverTargets" in fake.methods()
    detached = {m["params"]["sessionId"] for m in fake.received
                if m["method"] == "Target.detachFromTarget"}
    assert detached == {"S1", "S2"}


def test_a_second_client_waits_its_turn(broker, monkeypatch):
    _, port = broker
    first = WS(port)
    first.send({"id": 1, "method": "Target.getTargets"})
    first.recv()
    got = {}

    def second():
        ws = WS(port)
        got["status"] = ws.status
        ws.close()

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.3)
    assert "status" not in got          # held while the first client is connected
    first.close()
    t.join(5)
    assert "101" in got["status"]


# --- the launcher ---------------------------------------------------------------------


def test_pipe_launcher_relaunches_emdash_through_the_broker():
    lines = pair.pipe_launcher_script(9224, "/opt/bin/canopy-runner")
    assert lines[0].startswith('tell application "Emdash"')
    assert "emdash-pipe --port 9224" in lines[-1]
    assert "nohup" in lines[-1]
    assert "--remote-debugging-port" not in "\n".join(lines)
