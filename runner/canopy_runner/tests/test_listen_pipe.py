"""The runner carries a phone's encrypted meeting to canopy-listen and back. These pin that
frames pass through untouched both ways, that either side closing closes the other, and
that the control frame starts exactly one pipe per session."""
from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

websocket = pytest.importorskip("websocket")

from canopy_runner import listen_pipe  # noqa: E402


class FakeWS:
    """The server side of /ws/listen as the pipe sees it."""

    def __init__(self):
        self.inbox: queue.Queue = queue.Queue()  # what the server sends the laptop
        self.sent: list[bytes] = []  # what the laptop sent the server
        self.closed = threading.Event()
        self.url = None

    def recv_data(self):
        while True:
            if self.closed.is_set():
                return websocket.ABNF.OPCODE_CLOSE, b""
            try:
                return self.inbox.get(timeout=0.05)
            except queue.Empty:
                continue

    def send_binary(self, data):
        self.sent.append(bytes(data))

    def close(self):
        self.closed.set()


class FakeListen:
    """canopy-listen serve's socket: records the hello and frames, can answer and hang up."""

    def __init__(self, path: Path):
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(1)
        self.hello = None
        self.frames: list[bytes] = []
        self.conn = None
        self.ready = threading.Event()
        self.gone = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.conn, _ = self.server.accept()
        line = b""
        while not line.endswith(b"\n"):
            line += self.conn.recv(1)
        self.hello = json.loads(line)
        self.ready.set()
        while True:
            frame = listen_pipe.read_frame(self.conn)
            if frame is None:
                self.gone.set()
                return
            self.frames.append(frame)

    def send(self, data: bytes):
        listen_pipe.write_frame(self.conn, data)

    def hang_up(self):
        self.conn.shutdown(socket.SHUT_RDWR)
        self.conn.close()


@pytest.fixture()
def sock():
    d = tempfile.mkdtemp(prefix="lp", dir="/tmp")  # unix socket paths must be short
    yield Path(d) / "serve.sock"
    shutil.rmtree(d, ignore_errors=True)


def run_pipe(sock, ws, **kw):
    out = {}

    def go():
        out["why"] = listen_pipe.pipe("sid-1", "meeting", {"phone_eph": "AA"}, base_url="https://canopy.example",
                                      bearer=lambda: "tok", connect_ws=lambda url: setattr(ws, "url", url) or ws,
                                      path=sock, ensure=lambda p: True, **kw)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, out


def test_frames_pass_through_untouched_both_ways_and_the_hello_comes_first(sock):
    listen, ws = FakeListen(sock), FakeWS()
    t, out = run_pipe(sock, ws)
    assert listen.ready.wait(5)
    assert listen.hello == {"op": "session", "session_id": "sid-1", "kind": "meeting", "hello": {"phone_eph": "AA"}}
    assert ws.url == "wss://canopy.example/ws/listen/sid-1/?role=laptop"
    up = [os.urandom(n) for n in (40, 16000, 3)]
    for frame in up:
        ws.inbox.put((websocket.ABNF.OPCODE_BINARY, frame))
    listen.send(b"\x01down-1")
    for _ in range(100):
        if len(listen.frames) == 3 and ws.sent:
            break
        threading.Event().wait(0.02)
    assert listen.frames == up and ws.sent == [b"\x01down-1"]
    ws.inbox.put((websocket.ABNF.OPCODE_TEXT, json.dumps({"type": "ended", "reason": "ended_by_owner"})))
    t.join(5)
    assert out["why"] == "ended_by_owner" and listen.gone.wait(5)


def test_canopy_listen_hanging_up_closes_the_server_side(sock):
    listen, ws = FakeListen(sock), FakeWS()
    t, out = run_pipe(sock, ws)
    assert listen.ready.wait(5)
    listen.hang_up()
    t.join(5)
    assert out["why"] == "listen_closed" and ws.closed.is_set()


def test_no_canopy_listen_means_no_socket_opened(sock):
    opened = []
    why = listen_pipe.pipe("sid-2", "meeting", {}, base_url="https://x", bearer=lambda: "t",
                           connect_ws=lambda url: opened.append(url), path=sock, ensure=lambda p: False)
    assert why == "listen_not_running" and opened == []


def test_one_pipe_per_session_and_bad_frames_start_nothing(monkeypatch):
    started = []
    release = threading.Event()

    def fake_pipe(session_id, kind, hello, **kw):
        started.append(session_id)
        release.wait(5)

    monkeypatch.setattr(listen_pipe, "pipe", fake_pipe)
    msg = {"session_id": "s1", "kind": "meeting", "hello": {}}
    assert listen_pipe.start(msg, base_url="https://x", bearer=lambda: "t")
    assert not listen_pipe.start(msg, base_url="https://x", bearer=lambda: "t")
    assert not listen_pipe.start({"session_id": "s2", "kind": "shell", "hello": {}}, base_url="x", bearer=None)
    assert not listen_pipe.start({"session_id": "", "kind": "pair", "hello": {}}, base_url="x", bearer=None)
    release.set()


def test_the_control_frame_starts_a_pipe(monkeypatch):
    from canopy_runner import main

    seen = []
    monkeypatch.setattr(listen_pipe, "start", lambda msg, **kw: seen.append((msg["session_id"], kw["bearer"]())))

    class Cfg:
        base_url = "https://canopy.example"
        cdp_port = 0

    class Client:
        token = "bearer-now"

    handler = main.make_control_handler(Cfg(), waker=None, client=Client())
    handler({"type": "listen_start", "session_id": "s9", "kind": "meeting", "hello": {}})
    assert seen == [("s9", "bearer-now")]


def test_the_pending_poll_starts_each_waiting_session(monkeypatch):
    seen = []
    monkeypatch.setattr(listen_pipe, "start", lambda row, **kw: seen.append(row["session_id"]) or True)

    class Client:
        def _call_api(self, path, method, retry):
            assert path == "/runner/listen-sessions/pending" and method == "GET"
            return 200, [{"session_id": "a", "kind": "meeting", "hello": {}},
                         {"session_id": "b", "kind": "pair", "hello": {}}]

    assert listen_pipe.poll_pending(Client(), base_url="x", bearer=lambda: "t") == 2
    assert seen == ["a", "b"]
