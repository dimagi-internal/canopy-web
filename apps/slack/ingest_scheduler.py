"""The channel-ingest read's own clock: an asyncio task in every web process.

An approved ingest (apps/slack/ingest.py) reads a whole channel — thousands of
messages, paged, and paused whenever Slack rate-limits — which cannot happen
inside the 3 seconds Slack gives the Approve click. canopy-web has no worker or
queue, so this follows apps/capabilities/scheduler.py: a loop started in the
ASGI lifespan that calls `ingest.tick` every SLACK_INGEST_TICK_S, in a worker
thread with its DB connection closed before and after. `tick` leases each
ingest it takes, so N web tasks never read the same one at once.
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

    from . import ingest

    close_old_connections()
    try:
        ingest.tick()
        _expire_group_runs()
    finally:
        close_old_connections()


def _expire_group_runs() -> None:
    """Work group announcements nobody confirmed lapse after 24 h (apps/huddles/groups.py)
    — on this loop because it is the one that already reads channels for them."""
    from apps.huddles import groups

    try:
        groups.expire_due()
    except Exception:  # noqa: BLE001 — never stop the reads over it
        logger.exception("work group expiry failed")


async def run_forever(interval_s: float) -> None:
    await asyncio.sleep(random.uniform(1, max(1.0, interval_s)))  # noqa: S311
    while True:
        try:
            await sync_to_async(_tick, thread_sensitive=False)()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("slack channel ingest tick failed")
        await asyncio.sleep(interval_s)


@contextlib.asynccontextmanager
async def running():
    """Run the loop for as long as the context is open. SLACK_INGEST_TICK_S <= 0 turns it off."""
    interval = float(getattr(settings, "SLACK_INGEST_TICK_S", 15) or 0)
    if interval <= 0:
        yield
        return
    task = asyncio.create_task(run_forever(interval), name="slack-channel-ingest")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
