"""scripts/e2e_sensitive_redteam.py stays runnable: every step it advertises exists, a
refusal is read rather than raised (a 403 is usually the pass), and the canary never
leaves the run. The live run itself is the script's job, against production."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "e2e_sensitive_redteam.py"


@pytest.fixture(scope="module")
def rt():
    spec = importlib.util.spec_from_file_location("e2e_sensitive_redteam", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_step_has_a_method(rt):
    for step in (*rt.PREFLIGHT, *rt.EXITS):
        assert callable(getattr(rt.Run, f"step_{step}", None)), step


def test_every_probe_carries_the_canary(rt):
    assert all("{canary}" in template for _name, template in rt.PROBES)


@pytest.fixture()
def server():
    class Handler(BaseHTTPRequestHandler):
        def _answer(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps({"path": self.path}).encode()
            self.send_response(403 if "refuse" in self.path else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's names
            self._answer()

        def do_POST(self):  # noqa: N802
            self._answer()

        def do_PUT(self):  # noqa: N802
            self._answer()

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_a_refusal_is_read_not_raised(rt, server):
    api = rt.Client(server, "tok", "dimagi", "n1")
    assert api.raw("PUT", "/refuse/flags", {"flags": []}).status == 403
    assert api.raw("GET", "/ok").json() == {"path": "/api/ok"}
    with pytest.raises(rt.Failure):
        api.get("/refuse")


def test_the_dedicated_step_needs_a_probe_agent_and_sends_nothing_without_one(rt, server):
    class Args:
        probe_agent = ""
    run = rt.Run(rt.Client(server, "tok", "dimagi", "n2"), Args())
    with pytest.raises(rt.Failure, match="probe-agent"):
        run.step_dedicated()
