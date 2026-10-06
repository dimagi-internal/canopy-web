"""A stand-in for canopy-web's harness API, for driving ccd_runner end to end
against a REAL Claude desktop app without pairing a runner on labs.

It implements exactly the routes ccd_runner calls, with the same shapes:
heartbeat, claim, turns/{id}/start|events|transcript|finish, resolve-session,
record-session. Test-only extras: POST /enqueue (queue a turn) and GET /state.

    python3 fake_harness.py 8799            # then point a runner config at
                                            # http://127.0.0.1:8799 with token "fake"
"""
from __future__ import annotations

import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"queue": [], "turns": {}, "bindings": {}, "heartbeats": 0}
LOCK = threading.Lock()
PREFIX = "/api/harness"


class H(BaseHTTPRequestHandler):
    def log_message(self, *_a):  # quiet
        pass

    def _send(self, code: int, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/state":
            with LOCK:
                return self._send(200, STATE)
        self._send(404, {"detail": "nope"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        path = self.path.removeprefix(PREFIX)
        parts = [p for p in path.split("/") if p]
        with LOCK:
            if path == "/enqueue":
                turn = {"id": str(uuid.uuid4()), "project": body.get("project", ""),
                        "agent_slug": body.get("agent_slug", ""), "prompt": body["prompt"],
                        "origin_ref": body.get("origin_ref", {}), "workspace_slug": "dimagi"}
                STATE["queue"].append(turn)
                STATE["turns"][turn["id"]] = {"turn": turn, "status": "queued", "events": [],
                                              "transcript": [], "finish": None, "start": None}
                return self._send(201, turn)
            if parts[:1] == ["runners"] and parts[2:] == ["heartbeat"]:
                STATE["heartbeats"] += 1
                STATE["last_heartbeat"] = body
                return self._send(200, {})
            if parts[:1] == ["runners"] and parts[2:] == ["claim"]:
                if not STATE["queue"]:
                    return self._send(204)
                turn = STATE["queue"].pop(0)
                STATE["turns"][turn["id"]]["status"] = "claimed"
                return self._send(200, turn)
            if parts[:1] == ["runners"] and parts[2:] == ["record-session"]:
                STATE["bindings"][body["thread_key"]] = body["session_key"]
                return self._send(200, {})
            if parts[:1] == ["runners"] and parts[2:] == ["resolve-session"]:
                key = STATE["bindings"].get(body["thread_key"], "")
                return self._send(200, {"reuse": bool(key), "session_key": key, "summary": ""})
            if parts[:1] == ["turns"] and len(parts) == 3:
                t = STATE["turns"].get(parts[1])
                if t is None:
                    return self._send(404, {"detail": "turn not found"})
                verb = parts[2]
                if verb == "start":
                    t["start"], t["status"] = body, "running"
                elif verb == "events":
                    t["events"] += body.get("events", [])
                elif verb == "transcript":
                    t["transcript"] += body.get("lines", [])
                elif verb == "finish":
                    t["finish"], t["status"] = body, body.get("status")
                return self._send(200, {})
        self._send(404, {"detail": f"no route {self.path}"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
