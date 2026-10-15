"""The capability sweep's own clock: an asyncio task in every web process.

canopy-web has no scheduler or worker of its own (no beat, no queue, no
scheduled ECS task), and the sweep must not depend on the one periodic signal it
did have, a runner's claim: a fleet hold or no runner online would otherwise
stop every lease's expiry, idle release and orphan kill. So the ASGI app starts
this loop in its lifespan (config/asgi.py), next to the MCP session manager, and
the sweep runs off-request every CAPABILITY_SWEEP_INTERVAL_S in every web task.
`services.sweep_tick` deduplicates it fleet-wide through the cache (and falls
back to this process's own clock if the cache fails), so N tasks still sweep
about once per interval.

Why not an ECS scheduled task: a Fargate run-task of this image takes about a
minute to start, every interval, to do a second of work; the web task is always
up (DesiredCount 1, a rolling deploy keeps one healthy). The stack's own
backstop Lambda covers the case this cannot — canopy-web not running at all.

The sweep itself is synchronous ORM + boto3, so it runs in a worker thread
(`thread_sensitive=False`: it must not queue behind request handlers), with
its DB connection closed before and after like any request.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from asgiref.sync import sync_to_async
from django.conf import settings

logger = logging.getLogger(__name__)


def _tick() -> None:
    from django.db import close_old_connections

    from . import services

    close_old_connections()
    try:
        services.sweep_tick(trigger="scheduler")
    finally:
        close_old_connections()


async def run_forever(interval_s: float) -> None:
    # A random first delay, so a rolling deploy's two tasks do not tick in step.
    await asyncio.sleep(random.uniform(1, max(1.0, interval_s)))  # noqa: S311
    while True:
        try:
            await sync_to_async(_tick, thread_sensitive=False)()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — sweep_tick never raises; this is belt and braces
            logger.exception("capability sweep scheduler tick failed")
        await asyncio.sleep(interval_s)


@contextlib.asynccontextmanager
async def running():
    """Run the sweep loop for as long as the context is open (the ASGI lifespan).
    CAPABILITY_SWEEP_INTERVAL_S <= 0 turns it off."""
    interval = float(getattr(settings, "CAPABILITY_SWEEP_INTERVAL_S", 30) or 0)
    if interval <= 0:
        yield
        return
    task = asyncio.create_task(run_forever(interval), name="capability-sweep")
    logger.info("capability sweep scheduler started (every %ss)", interval)
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
