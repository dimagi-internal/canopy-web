"""No log line carries a client address, a concrete path or a query string.

A path can be a bearer link (`/share/<token>`, a walkthrough's or storyboard's
link) and a query a live credential (a WebSocket's `?ticket=`); canopy-web's logs
land in CloudWatch. What a line may say about a request is its method, route
TEMPLATE, status and timing (apps/common/log_redact.py).
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
import sys

import pytest
from django.http import HttpResponse
from django.test import RequestFactory
from django.utils.log import log_response

from apps.common.log_format import JsonFormatter
from apps.common.log_redact import RedactRequestDetail, path_template

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKEN = "Zq9xK2vB7mLwR4tYp8sN3cHf6dJ1aE5g"
TICKET = "tkt-Secret-Value-123"
CLIENT = "203.0.113.7:51234"


def _capture(logger_name: str):
    """A handler wired like production's console: redact filter + JSON."""
    lines: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    handler = _Handler()
    handler.addFilter(RedactRequestDetail())
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    return logger, handler, lines


def _assert_clean(text: str):
    for secret in (TOKEN, TICKET, "203.0.113.7", "q=private"):
        assert secret not in text, f"{secret!r} leaked into a log line: {text}"


def test_the_container_runs_uvicorn_without_its_access_log():
    cmd = (ROOT / "Dockerfile").read_text().split("\nCMD [", 1)[1].split("]", 1)[0]
    assert '"--no-access-log"' in cmd, cmd


def test_an_access_log_record_is_dropped():
    logger, handler, lines = _capture("uvicorn.access")
    try:
        logger.propagate, old = False, logger.propagate
        logger.setLevel(logging.INFO)
        logger.info('%s - "%s %s HTTP/%s" %d', CLIENT, "GET", f"/share/{TOKEN}?q=private", "1.1", 200)
    finally:
        logger.removeHandler(handler)
        logger.propagate = old
    assert lines == []


def test_a_websocket_handshake_line_is_folded():
    logger, handler, lines = _capture("uvicorn.error")
    try:
        logger.setLevel(logging.INFO)
        logger.info('%s - "WebSocket %s" [accepted]', CLIENT,
                    f"/ws/turns/117ee3fb-979c-41d3-988f-58420bc422f3/?ticket={TICKET}")
    finally:
        logger.removeHandler(handler)
    assert len(lines) == 1
    _assert_clean(lines[0])
    assert json.loads(lines[0])["message"] == '- - "WebSocket /ws/turns/<x>/" [accepted]'


@pytest.mark.django_db
def test_a_404_on_a_share_link_logs_the_template_not_the_token():
    request = RequestFactory().get(f"/share/{TOKEN}", {"q": "private"})
    logger, handler, lines = _capture("django.request")
    try:
        log_response("Not Found: %s", request.path, response=HttpResponse(status=404),
                     request=request)
    finally:
        logger.removeHandler(handler)
    assert len(lines) == 1
    _assert_clean(lines[0])
    out = json.loads(lines[0])
    assert out["message"] == "Not Found: /share/<x>"
    assert out["request"] == "GET /share/<x>"


def test_a_matched_route_logs_its_template():
    class _Match:
        route = "w/<str:ws>/walkthrough/<str:wid>/content"

    class _Req:
        resolver_match = _Match()

    assert path_template(f"/w/acme/walkthrough/{TOKEN}/content", _Req()) == \
        "/w/<str:ws>/walkthrough/<str:wid>/content"


def test_production_logging_wires_the_filter_and_silences_the_access_log():
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.production",
           "ALLOWED_HOSTS": "example.com", "SECRET_KEY": "a-real-strong-secret-value-not-the-default"}
    out = subprocess.run(
        [sys.executable, "-c", "import json; from django.conf import settings; print(json.dumps(settings.LOGGING))"],
        env=env, capture_output=True, text=True, cwd=ROOT,
    )
    assert out.returncode == 0, out.stderr
    config = json.loads(out.stdout.strip().splitlines()[-1])
    for name, handler in config["handlers"].items():
        assert "redact" in handler.get("filters", []), name
    assert config["loggers"]["uvicorn.access"]["level"] == "CRITICAL"
    assert config["loggers"]["uvicorn.access"]["handlers"] == []
    assert "redact" in config["loggers"]["uvicorn.error"]["filters"]


def test_development_logging_wires_the_filter_too():
    from django.conf import settings

    for name, handler in settings.LOGGING["handlers"].items():
        assert "redact" in handler.get("filters", []), name
    assert "redact" in settings.LOGGING["loggers"]["uvicorn.error"]["filters"]
