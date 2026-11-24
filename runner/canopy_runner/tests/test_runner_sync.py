"""The runner reads its work through `/sync` when `v` moves (runner_sync.py), beats
over the control socket, and claims on a wake or the safety clock."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from canopy_runner import emdash
from canopy_runner import main as main_mod
from canopy_runner.cancel import CANCELLED_TURNS
from canopy_runner.client import Client, ClientError
from canopy_runner.config import Config
from canopy_runner.runner_sync import RECONCILE_SECONDS, UNSUPPORTED_RETRY_SECONDS, SyncBox


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class _Server:
    """canopy-web's `/sync`: a version and the rows pending at it."""

    def __init__(self, status=None):
        self.version = 1
        self.rows = {"closes": [{"session_key": "k1", "project": "p"}],
                     "streams": [{"session_id": "s1"}]}
        self.reads = []
        self.status = status  # e.g. 404: a server without the route

    def get_sync(self, runner_id, etag=""):
        self.reads.append(etag)
        if self.status:
            raise ClientError(f"GET /sync -> {self.status}", status=self.status)
        tag = f'"v{self.version}"'
        if etag == tag:
            return 304, tag, None
        return 200, tag, {"version": self.version, **self.rows}


# ---- SyncBox ------------------------------------------------------------------


def test_reads_once_then_only_when_v_moves():
    clock, srv = _Clock(), _Server()
    box = SyncBox(clock)
    assert box.refresh(srv, "r") is True
    assert box.active
    clock.t += 5
    assert box.refresh(srv, "r") is False  # nothing moved: no read at all
    assert len(srv.reads) == 1
    srv.version = 2
    box.see(2)  # `v` on a heartbeat reply, an ack or a frame
    assert box.refresh(srv, "r") is True
    assert srv.reads[-1] == '"v1"'  # a conditional read


def test_an_unchanged_version_answers_304_and_keeps_the_answer():
    clock, srv = _Clock(), _Server()
    box = SyncBox(clock)
    box.refresh(srv, "r")
    box.nudge()  # a doorbell whose write did not move the version
    assert box.refresh(srv, "r") is False
    assert srv.reads == ["", '"v1"']
    assert box.streams() == [{"session_id": "s1"}]


def test_a_dropped_frame_is_caught_by_the_reconcile():
    """Work recorded while the frame and every `v` were lost is read at the reconcile."""
    clock, srv = _Clock(), _Server()
    box = SyncBox(clock)
    box.refresh(srv, "r")
    assert box.take("closes")
    srv.version = 2
    srv.rows["closes"] = [{"session_key": "k2", "project": "p"}]
    clock.t += RECONCILE_SECONDS - 1
    assert box.refresh(srv, "r") is False
    clock.t += 2
    assert box.refresh(srv, "r") is True
    assert srv.reads[-1] == ""  # unconditional
    assert box.take("closes") == [{"session_key": "k2", "project": "p"}]


def test_the_reconcile_hands_back_work_a_drain_could_not_finish():
    clock, srv = _Clock(), _Server()
    box = SyncBox(clock)
    box.refresh(srv, "r")
    box.take("closes")
    clock.t += RECONCILE_SECONDS
    box.refresh(srv, "r")  # same version, unconditional: the close is still pending
    assert box.take("closes") == [{"session_key": "k1", "project": "p"}]


def test_edge_rows_are_handed_out_once_and_streams_every_tick():
    box = SyncBox(_Clock())
    box.refresh(_Server(), "r")
    assert box.take("closes") and box.take("closes") == []
    assert box.streams() == box.streams() == [{"session_id": "s1"}]


def test_a_server_without_sync_leaves_the_drains_on_their_routes():
    clock, srv = _Clock(), _Server(status=404)
    box = SyncBox(clock)
    assert box.refresh(srv, "r") is False
    assert box.supported is False and not box.active
    clock.t += 5
    box.refresh(srv, "r")
    assert len(srv.reads) == 1  # not asked again every tick
    clock.t += UNSUPPORTED_RETRY_SECONDS
    srv.status = None
    assert box.refresh(srv, "r") is True and box.active


def test_the_client_drains_read_the_sync_answer_or_their_routes(monkeypatch):
    c = Client("http://x", "tok")
    routes = []
    monkeypatch.setattr(c, "_call", lambda method, path, body=None, **k: (
        routes.append(path), (200, {"closes": [{"session_key": "route"}]}))[1])
    assert c.sync_closes("r") == [{"session_key": "route"}]  # no box: the route
    c.sync_box = SyncBox(_Clock())
    c.sync_box.refresh(_Server(), "r")
    assert c.sync_closes("r") == [{"session_key": "k1", "project": "p"}]
    assert c.sync_closes("r") == []
    assert routes == ["/runners/r/closes"]


def test_a_new_session_in_the_report_makes_the_next_read_unconditional(monkeypatch):
    c = Client("http://x", "tok")
    monkeypatch.setattr(c, "_call", lambda *a, **k: (200, {}))
    clock, srv = _Clock(), _Server()
    c.sync_box = SyncBox(clock)
    c.sync_box.refresh(srv, "r")
    c.report_sessions("r", [{"project": "p", "emdash_task": "t1"}])
    assert c.sync_box.refresh(srv, "r") is True
    assert srv.reads[-1] == ""
    c.report_sessions("r", [{"project": "p", "emdash_task": "t1"}])  # same set
    assert c.sync_box.refresh(srv, "r") is False


# ---- Client.get_sync over HTTP --------------------------------------------------


class _SyncHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if not self.path.endswith("/sync"):
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("If-None-Match") == '"v3"':
            self.send_response(304)
            self.send_header("ETag", '"v3"')
            self.end_headers()
            return
        body = json.dumps({"version": 3}).encode()
        self.send_response(200)
        self.send_header("ETag", '"v3"')
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def sync_server():
    srv = HTTPServer(("127.0.0.1", 0), _SyncHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_get_sync_sends_the_etag_and_reads_a_304(sync_server):
    c = Client(sync_server, "tok")
    assert c.get_sync("r") == (200, '"v3"', {"version": 3})
    assert c.get_sync("r", '"v3"') == (304, '"v3"', None)


def test_get_sync_on_a_server_without_it_raises_its_status():
    srv = HTTPServer(("127.0.0.1", 0), _SyncHandler)
    srv.RequestHandlerClass = type("H", (_SyncHandler,), {"do_GET": lambda self: (
        self.send_response(404), self.end_headers())})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ClientError) as err:
            Client(f"http://127.0.0.1:{srv.server_port}", "tok").get_sync("r")
        assert err.value.status == 404
    finally:
        srv.shutdown()


# ---- the loop: heartbeat transport, claim cadence ------------------------------


class _Socket:
    def __init__(self, connected=True):
        self.connected = connected
        self.sent = []
        self.wakes = 0
        self.acks = 0
        self.last_ack = None

    def send_json(self, msg):
        self.sent.append(msg)
        return self.connected


class _LoopClient:
    def __init__(self, box):
        self.sync_box = box
        self.beats = 0
        self.claims = 0

    def heartbeat(self, runner_id, active, **kw):
        self.beats += 1
        return {"status": "online"}

    def heartbeat_body(self, active, **kw):
        return {"active_turn_ids": active, "sync": 1}

    def report_sessions(self, *a, **k):
        pass

    def claim(self, runner_id, paused_agents=None):
        self.claims += 1
        return None


@pytest.fixture
def loop(monkeypatch, tmp_path):
    monkeypatch.setattr("canopy_runner.cdp_control.cdp_healthy", lambda **k: True)
    monkeypatch.setattr("canopy_runner.cdp_control.host_id", lambda: "u@h")
    monkeypatch.setattr(emdash, "list_open_sessions", lambda db: [])
    clock = _Clock()
    monkeypatch.setattr(main_mod.time, "monotonic", clock)
    box = SyncBox(clock)
    box.supported = True
    monkeypatch.setattr(box, "refresh", lambda client, rid: False)
    cfg = Config(base_url="http://x", token="t", runner_id="r-1", emdash_db=str(tmp_path / "e.db"))
    return cfg, clock, _LoopClient(box)


def test_with_the_socket_up_the_beat_rides_it_and_rest_refreshes_the_reply(loop, monkeypatch):
    cfg, clock, client = loop
    sock = _Socket()
    monkeypatch.setattr(main_mod, "_control", sock)
    main_mod.run_once(cfg, client)  # the first beat is REST: the reply the observers read
    assert (client.beats, len(sock.sent)) == (1, 0)
    for _ in range(10):  # 10 ticks of 5s: one socket beat each heartbeat_seconds
        clock.t += 5
        main_mod.run_once(cfg, client)
    assert client.beats == 1
    assert [m["action"] for m in sock.sent] == ["heartbeat"]
    clock.t += main_mod.REST_REFRESH_SECONDS
    main_mod.run_once(cfg, client)
    assert client.beats == 2


def test_without_the_socket_the_rest_beat_carries_v_every_15s(loop, monkeypatch):
    cfg, clock, client = loop
    monkeypatch.setattr(main_mod, "_control", _Socket(connected=False))
    main_mod.run_once(cfg, client)
    for _ in range(6):
        clock.t += 5
        main_mod.run_once(cfg, client)
    assert client.beats == 3  # t=0, 15, 30


def test_a_socket_ack_folds_into_the_reply_and_its_stops_are_honoured(loop, monkeypatch):
    cfg, clock, client = loop
    sock = _Socket()
    monkeypatch.setattr(main_mod, "_control", sock)
    main_mod.run_once(cfg, client)
    sock.last_ack, sock.acks = {"type": "heartbeat.ack", "cancel_turn_ids": ["t-9"], "v": 4}, 1
    clock.t += 5
    main_mod.run_once(cfg, client)
    assert "t-9" in CANCELLED_TURNS
    CANCELLED_TURNS.discard("t-9")


def test_with_sync_and_the_socket_the_claim_waits_for_a_wake_or_the_safety_clock(loop, monkeypatch):
    cfg, clock, client = loop
    sock = _Socket()
    monkeypatch.setattr(main_mod, "_control", sock)
    main_mod.run_once(cfg, client)
    for _ in range(5):
        clock.t += 5
        main_mod.run_once(cfg, client)
    assert client.claims == 1
    sock.wakes += 1  # a turn was queued
    main_mod.run_once(cfg, client)
    assert client.claims == 2
    clock.t += main_mod.SAFETY_CLAIM_SECONDS
    main_mod.run_once(cfg, client)
    assert client.claims == 3


def test_without_sync_the_claim_runs_every_tick(loop, monkeypatch):
    cfg, clock, client = loop
    client.sync_box.supported = False
    monkeypatch.setattr(main_mod, "_control", _Socket())
    for _ in range(3):
        main_mod.run_once(cfg, client)
        clock.t += 5
    assert client.claims == 3
