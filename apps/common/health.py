"""`/health/`, answered in the ASGI layer so a busy Django cannot starve it.

The ALB checks every 5s with a 4s timeout. A check that went through Django
would wait for a request thread, the middleware stack and the worker pool like
any page, so a burst of slow requests would make a working task look dead and
ECS would replace it, turning slowness into 502s.

`HealthMiddleware` answers GET/HEAD `/health/` on the event loop, before routing.
It still fails when the process is genuinely stuck, on two signals:

* the event loop is blocked — then nothing answers at all and the ALB times out;
* the worker pool makes no progress — `heartbeat()` runs a no-op through
  `sync_to_async(thread_sensitive=False)` (the pool the off-request ticks and
  many sync calls share) every `BEAT_S`; when the last one finished more than
  `STALL_S` ago the check answers 503.

Before the heartbeat has started (no lifespan: tests, `manage.py`) the check
answers 200. The body is the Django view's (config/views.py::health_check).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time

from asgiref.sync import sync_to_async
from django.conf import settings

logger = logging.getLogger(__name__)

PATHS = ("/health/",)
BEAT_S = 5.0
#: Seconds without a finished heartbeat before the check fails.
STALL_S = float(os.environ.get("CANOPY_HEALTH_STALL_S", "60"))

#: monotonic time the last heartbeat finished; None until the heartbeat starts.
_last_beat: float | None = None


def _noop() -> None:
    return None


def stalled_for(now: float | None = None) -> float:
    """Seconds since the last heartbeat finished (0 before it has started)."""
    if _last_beat is None:
        return 0.0
    return max(0.0, (time.monotonic() if now is None else now) - _last_beat)


def _body() -> tuple[int, dict]:
    stalled = stalled_for()
    if stalled > STALL_S:
        return 503, {"status": "stalled", "stalled_s": int(stalled)}
    # The runner release this deployment ships: `apps.harness.runner_releases.deployed_sha`,
    # read from settings here because common sits below harness.
    return 200, {"status": "ok", "runner_code_sha": (getattr(settings, "RUNNER_CODE_SHA", "") or "").strip()}


async def _beat_forever() -> None:
    global _last_beat
    while True:
        await asyncio.sleep(BEAT_S)
        try:
            await sync_to_async(_noop, thread_sensitive=False)()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a heartbeat never stops beating
            logger.exception("health heartbeat failed")
            continue
        _last_beat = time.monotonic()


@contextlib.asynccontextmanager
async def heartbeat():
    """Run the heartbeat for as long as the context is open (the ASGI lifespan)."""
    global _last_beat
    _last_beat = time.monotonic()
    task = asyncio.create_task(_beat_forever(), name="health-heartbeat")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        _last_beat = None


class HealthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("path") not in PATHS
                or scope.get("method") not in ("GET", "HEAD")):
            return await self.app(scope, receive, send)
        status, payload = _body()
        body = json.dumps(payload).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": b"" if scope["method"] == "HEAD" else body})
