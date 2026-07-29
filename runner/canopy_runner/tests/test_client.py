import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from canopy_runner.client import Client, ClientError


class _Handler(BaseHTTPRequestHandler):
    # Records the (path, body) of the last schedule call so the tests can pin the
    # exact wire format against the server contract (apps/harness/api.py).
    last_fire = None

    def do_GET(self):
        assert self.headers["Authorization"] == "Bearer tok"
        if self.path.startswith("/api/harness/schedules/"):
            _Handler.last_sync_path = self.path
            # Shape mirrors Page[ScheduleOut] — the runner wants .items.
            body = json.dumps({
                "items": [{"id": 7, "agent_slug": "echo", "cron": "0 9 * * 5",
                           "timezone": "America/New_York", "enabled": True,
                           "fire_after": "2026-07-08T12:00:00+00:00"}],
                "total": 1, "offset": 0, "limit": 200,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(500)
        self.end_headers()

    def do_POST(self):
        assert self.headers["Authorization"] == "Bearer tok"
        if "/fire" in self.path:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            _Handler.last_fire = (self.path, json.loads(raw))
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"id": "turn-9", "status": "queued"}')
            return
        if self.path.endswith("/claim"):
            if _Handler.claim_empty:
                self.send_response(204); self.end_headers(); return
            body = json.dumps({"id": "t-1", "status": "claimed"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.endswith("/heartbeat"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "online"}')
            return
        self.send_response(500); self.end_headers()

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture()
def server():
    _Handler.claim_empty = False
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_claim_returns_turn(server):
    c = Client(server, "tok")
    turn = c.claim("r-1")
    assert turn["id"] == "t-1"


def test_claim_returns_none_on_204(server):
    _Handler.claim_empty = True
    c = Client(server, "tok")
    assert c.claim("r-1") is None


def test_error_raises(server):
    c = Client(server, "tok")
    with pytest.raises(ClientError):
        c.post_events("t-1", [{"kind": "status", "payload": {}}])  # handler 500s unknown paths


def test_sync_schedules_unwraps_page_items(server):
    c = Client(server, "tok")
    items = c.sync_schedules("r-1")
    assert [s["id"] for s in items] == [7]
    # runner_id is a query param on the runner-facing route (not a path segment).
    assert _Handler.last_sync_path == "/api/harness/schedules/?runner_id=r-1"


def test_fire_schedule_posts_slot_with_runner_id(server):
    c = Client(server, "tok")
    turn = c.fire_schedule(7, "r-1", "2026-07-10T13:00:00+00:00")
    assert turn["id"] == "turn-9"
    path, body = _Handler.last_fire
    assert path == "/api/harness/schedules/7/fire?runner_id=r-1"
    assert body == {"slot": "2026-07-10T13:00:00+00:00"}
