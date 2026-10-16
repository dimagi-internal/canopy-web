"""The canopy-pat rotation's own clock: an asyncio task in every web process
(Canopy Vault Phase 1b, canopy-web#1549).

canopy-web has no worker, beat or scheduled task, so this follows
apps/capabilities/scheduler.py and apps/slack/ingest_scheduler.py: a loop
started in the ASGI lifespan (config/asgi.py) that calls
`pat_rotation.tick_if_due` every CANOPY_PAT_ROTATION_TICK_S (default 900 s), in
a worker thread with its DB connection closed before and after. The cache lock
in `tick_if_due` keeps N web tasks to about one tick per interval, and the agent
row lock in `rotate` keeps two from rotating the same agent. Rotation is daily
work at most, so a 15-minute tick is plenty; the overlap (24 h) is what keeps a
late tick harmless. `manage.py rotate_canopy_pats` runs one tick by hand.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)


def _tick(interval_s: float) -> None:
    from django.db import close_old_connections

    from . import pat_rotation

    close_old_connections()
    try:
        pat_rotation.tick_if_due(interval_s)
    finally:
        close_old_connections()


async def run_forever(interval_s: float) -> None:
    await asyncio.sleep(random.uniform(1, max(1.0, min(interval_s, 60.0))))  # noqa: S311
    while True:
        try:
            await sync_to_async(_tick, thread_sensitive=False)(interval_s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — tick never raises; belt and braces
            logger.exception("canopy-pat rotation tick failed")
        await asyncio.sleep(interval_s)


@contextlib.asynccontextmanager
async def running():
    """Run the loop for as long as the context is open. CANOPY_PAT_ROTATION_TICK_S <= 0 turns it off."""
    from . import pat_rotation

    interval = pat_rotation.interval_s()
    if interval <= 0:
        yield
        return
    task = asyncio.create_task(run_forever(interval), name="canopy-pat-rotation")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
