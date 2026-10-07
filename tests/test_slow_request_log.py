"""A request over the threshold is logged with its route and its database share."""
from __future__ import annotations

import logging

import pytest
from django.http import HttpResponse
from django.test import RequestFactory

from apps.common import request_timing
from apps.common.request_timing import SlowRequestLogMiddleware, route_shape

pytestmark = pytest.mark.django_db


def test_route_shape_folds_ids():
    assert route_shape("/canopy/api/harness/runners/117ee3fb-979c-41d3-988f-58420bc422f3/claim") == \
        "/canopy/api/harness/runners/<id>/claim"
    assert route_shape("/api/agents/eva/actions/42/applied") == "/api/agents/eva/actions/<n>/applied"


def test_logs_a_slow_request_and_not_a_fast_one(monkeypatch, caplog):
    rf = RequestFactory()
    mw = SlowRequestLogMiddleware(lambda r: HttpResponse(status=200))
    monkeypatch.setattr(request_timing, "THRESHOLD_MS", 10_000)
    with caplog.at_level(logging.WARNING, logger="canopy.slow_requests"):
        mw(rf.get("/api/me/"))
    assert "SLOW_REQUEST" not in caplog.text
    monkeypatch.setattr(request_timing, "THRESHOLD_MS", 1)
    times = iter([0.0, 2.5])
    monkeypatch.setattr(request_timing.time, "perf_counter", lambda: next(times))
    with caplog.at_level(logging.WARNING, logger="canopy.slow_requests"):
        mw(rf.get("/api/me/"))
    assert "SLOW_REQUEST GET /api/me/ status=200 ms=2500" in caplog.text
