"""`/health/` is answered in the ASGI layer (apps/common/health.py), and fails
only when the worker pool has made no progress for STALL_S."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from asgiref.sync import async_to_sync

from apps.common import health


def _call(path, method="GET", app=None):
    sent, reached = [], []

    async def inner(scope, receive, send):
        reached.append(scope["path"])

    async def send(message):
        sent.append(message)

    async def go():
        await health.HealthMiddleware(app or inner)(
            {"type": "http", "path": path, "method": method}, None, send)

    async_to_sync(go)()
    if not sent:
        return None, None, reached
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], body, reached


@pytest.fixture(autouse=True)
def _reset():
    health._last_beat = None
    yield
    health._last_beat = None


def test_answers_without_reaching_the_app(settings):
    settings.RUNNER_CODE_SHA = "abc123"
    status, body, reached = _call("/health/")
    assert status == 200
    assert json.loads(body) == {"status": "ok", "runner_code_sha": "abc123"}
    assert reached == []


def test_head_has_no_body():
    status, body, _ = _call("/health/", method="HEAD")
    assert status == 200 and body == b""


def test_other_paths_and_methods_pass_through():
    for path, method in (("/api/me/", "GET"), ("/health/x", "GET"), ("/health/", "POST")):
        status, _, reached = _call(path, method)
        assert status is None and reached == [path]


def test_a_recent_heartbeat_is_healthy():
    health._last_beat = time.monotonic() - 5
    assert _call("/health/")[0] == 200


def test_a_stalled_worker_pool_fails_the_check():
    health._last_beat = time.monotonic() - health.STALL_S - 5
    status, body, _ = _call("/health/")
    assert status == 503
    assert json.loads(body)["status"] == "stalled"


def test_the_heartbeat_beats_and_stops(monkeypatch):
    monkeypatch.setattr(health, "BEAT_S", 0.01)

    async def go():
        async with health.heartbeat():
            started = health._last_beat
            assert started is not None
            await asyncio.sleep(0.2)
            assert health._last_beat > started
        assert health._last_beat is None

    async_to_sync(go)()


def test_the_asgi_application_serves_it_under_the_old_prefix(monkeypatch):
    """The ALB path is /health/; the old address arrives as /canopy/health/."""
    from config.asgi import application
    from config.asgi_prefix import StripScriptName

    sent = []

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request", "body": b""}

    async def go():
        scope = {"type": "http", "path": "/canopy/health/", "raw_path": b"/canopy/health/",
                 "method": "GET", "headers": [], "query_string": b"", "root_path": ""}
        await StripScriptName(application, "/canopy")(scope, receive, send)

    async_to_sync(go)()
    assert sent[0]["status"] == 200
