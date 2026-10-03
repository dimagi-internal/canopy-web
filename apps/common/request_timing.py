"""Log every request that takes longer than a threshold, with what it spent.

Labs serves everything from one uvicorn process, so one slow request stalls the
ones queued behind it; on 2026-10-03 `/api/me/` — a 0.3s read every page waits
on — took 1.4s at the median and up to 30s, at 10-20% CPU. The access log says
which paths were hit and never how long they took, so the request doing the
stalling could not be named. This names it: path, status, wall time, and how
much of that was the database (count + time), at WARNING so it is greppable in
CloudWatch (`SLOW_REQUEST`).

A request that merely WAITED behind a slow one is not slow inside Django and is
not logged — which is the point: the lines that appear are the culprits.
"""
from __future__ import annotations

import logging
import os
import re
import time

from django.db import connection

logger = logging.getLogger("canopy.slow_requests")

# Milliseconds. 0 disables. Overridable per deployment without a code change.
THRESHOLD_MS = int(os.environ.get("CANOPY_SLOW_REQUEST_MS", "1000"))

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def route_shape(path: str) -> str:
    """The path with ids folded, so the same route groups in a log query."""
    return re.sub(r"/\d+(?=/|$)", "/<n>", _UUID.sub("<id>", path))


class SlowRequestLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if THRESHOLD_MS <= 0:
            return self.get_response(request)
        db = {"n": 0, "s": 0.0}

        def count(execute, sql, params, many, context):
            t = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                db["n"] += 1
                db["s"] += time.perf_counter() - t

        start = time.perf_counter()
        with connection.execute_wrapper(count):
            response = self.get_response(request)
        ms = (time.perf_counter() - start) * 1000
        if ms >= THRESHOLD_MS:
            logger.warning(
                "SLOW_REQUEST %s %s status=%s ms=%d db_queries=%d db_ms=%d",
                request.method, route_shape(request.path), getattr(response, "status_code", "?"),
                ms, db["n"], db["s"] * 1000,
            )
        return response
