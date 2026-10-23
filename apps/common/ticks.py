"""canopy-web's one off-request clock: periodic jobs, run in every web process.

canopy-web has no worker, beat or scheduled task, and periodic work must not
ride a request — least of all a runner's heartbeat or claim, which stop with a
fleet hold, a paused fleet or no runner online. So each app registers its jobs
here (`register`, from its AppConfig.ready), and the ASGI lifespan
(config/asgi.py) runs ONE loop, `running()`, that wakes every TICK_BASE_S and
runs each job that has come due, in a worker thread (`thread_sensitive=False`:
it must not queue behind request handlers) with its DB connection closed before
and after, like any request.

Fleet-wide, a job runs about once per interval however many web tasks there
are: a due job first takes a cache key (`cache.add`, expiring with the
interval). If the cache is down, this process runs it on its own clock rather
than skipping — a cost or expiry guard must not depend on Redis. A job that
deduplicates itself (its own lock, as the capability sweep does) registers with
`dedupe=False`. A job never raises out of a tick: one failing job is logged and
the rest still run.

This module is framework code and names no app: the jobs, and why each runs
when it does, live with the app that registers them. `manage.py run_ticks`
runs one pass by hand.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

CACHE_PREFIX = "ticks:"


@dataclass
class Job:
    name: str
    fn: Callable[[], object]
    #: Seconds between runs, or a callable reading it from settings at tick time.
    #: <= 0 turns the job off.
    every_s: float | Callable[[], float]
    dedupe: bool = True
    last_run: float = field(default=float("-inf"))

    def interval(self) -> float:
        value = self.every_s() if callable(self.every_s) else self.every_s
        return float(value or 0)


_JOBS: dict[str, Job] = {}
#: The event loop the clock runs on (set by `running`), for a job that reports on it.
_LOOP: asyncio.AbstractEventLoop | None = None


def loop() -> asyncio.AbstractEventLoop | None:
    return _LOOP


def register(name: str, fn: Callable[[], object], every_s: float | Callable[[], float], *,
             dedupe: bool = True) -> None:
    """Run `fn()` about every `every_s` seconds, fleet-wide. Re-registering a
    name replaces it (an app's ready() may run more than once in tests)."""
    _JOBS[name] = Job(name, fn, every_s, dedupe)


def jobs() -> dict[str, Job]:
    return dict(_JOBS)


def _monotonic() -> float:
    return time.monotonic()


def _claim(job: Job, interval: float) -> bool:
    """Whether THIS process runs the job now: the fleet-wide cache key, else
    this process's own clock when the cache is unavailable."""
    if not job.dedupe:
        return True
    try:
        return bool(cache.add(CACHE_PREFIX + job.name, timezone.now().isoformat(),
                              max(1, int(interval) - 1)))
    except Exception as e:  # noqa: BLE001 — Redis down: run on this process's clock
        logger.warning("tick %s: the cache is unavailable (%s); using this process's clock", job.name, e)
        return True


def run_job(job: Job) -> bool:
    """Run one job if it is due. Never raises. True when it ran."""
    interval = job.interval()
    if interval <= 0:
        return False
    now = _monotonic()
    if now - job.last_run < interval:
        return False
    job.last_run = now
    try:
        if not _claim(job, interval):
            return False
        job.fn()
        return True
    except Exception:  # noqa: BLE001 — one job must never stop the rest
        logger.exception("tick %s failed", job.name)
        return False


def tick() -> list[str]:
    """One pass: every due job, in registration order. Returns the names that ran."""
    return [job.name for job in list(_JOBS.values()) if run_job(job)]


def _tick_off_request() -> None:
    """`tick` as the loop runs it: its DB connection closed before and after, like
    any request."""
    from django.db import close_old_connections

    close_old_connections()
    try:
        tick()
    finally:
        close_old_connections()


def base_interval_s() -> float:
    return float(getattr(settings, "TICK_BASE_S", 5) or 0)


async def run_forever(base_s: float) -> None:
    # A random first delay, so a rolling deploy's two tasks do not tick in step.
    await asyncio.sleep(random.uniform(1, max(1.0, base_s)))  # noqa: S311
    while True:
        try:
            await sync_to_async(_tick_off_request, thread_sensitive=False)()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — tick never raises; belt and braces
            logger.exception("tick loop failed")
        await asyncio.sleep(base_s)


@contextlib.asynccontextmanager
async def running():
    """Run the loop for as long as the context is open (the ASGI lifespan).
    TICK_BASE_S <= 0 turns every job off."""
    base = base_interval_s()
    if base <= 0:
        yield
        return
    global _LOOP
    _LOOP = asyncio.get_running_loop()
    task = asyncio.create_task(run_forever(base), name="ticks")
    logger.info("ticks started (every %ss): %s", base, ", ".join(_JOBS))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
