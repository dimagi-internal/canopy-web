"""The capability sweep's clock: a job on the off-request clock (apps/common/ticks.py).

The sweep must not depend on the one periodic signal canopy-web used to have, a
runner's claim: a fleet hold or no runner online would otherwise stop every
lease's expiry, idle release and orphan kill. So it runs off-request every
CAPABILITY_SWEEP_INTERVAL_S in every web process (registered from
CapabilitiesConfig.ready). `services.sweep_tick` deduplicates it fleet-wide
through the cache (and falls back to this process's own clock if the cache
fails), so N tasks still sweep about once per interval.

Why not an ECS scheduled task: a Fargate run-task of this image takes about a
minute to start, every interval, to do a second of work; the web task is always
up (DesiredCount 1, a rolling deploy keeps one healthy). The stack's own
backstop Lambda covers the case this cannot — canopy-web not running at all.
"""
from __future__ import annotations

from django.conf import settings


def interval_s() -> float:
    return float(getattr(settings, "CAPABILITY_SWEEP_INTERVAL_S", 30) or 0)


def run() -> None:
    from . import services

    services.sweep_tick(trigger="scheduler")


def register() -> None:
    from apps.common import ticks

    ticks.register("capabilities.sweep", run, interval_s, dedupe=False)
