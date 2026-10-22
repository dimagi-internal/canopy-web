"""The harness's periodic jobs, run on the off-request clock (apps/common/ticks.py).

They used to ride runner traffic — the queue sweeps every claim poll, the
scheduled unpause every heartbeat — so they stopped whenever no runner was
polling. Registered from HarnessConfig.ready().
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from .claim import sweep_expired_leases
from .drills import expire_stale_drills
from .models import FleetHold
from .schedule_turns import release_stale_occurrence_turns_all, skip_late_scheduled_turns
from .services import wake_due_runners as _wake_due_runners

logger = logging.getLogger(__name__)


def queue_sweeps_every_s() -> float:
    return float(getattr(settings, "HARNESS_QUEUE_SWEEP_TICK_S", 15) or 0)


def queue_sweeps() -> None:
    """Close what can no longer run: expired leases, stale schedule-occurrence
    turns, scheduled slots too late to fire (MISSED), stale readiness drills.

    Not while the FLEET-WIDE hold is on: `skip_late_scheduled_turns` would close a
    held schedule slot as MISSED after 30 minutes, erasing it from the very queue
    the hold exists to show. On release the next tick runs them, so stale slots
    close then instead of firing as a burst. Each sweep is guarded on its own: one
    failing must not stop the rest."""
    if FleetHold.is_held():
        return
    for sweep in (sweep_expired_leases, release_stale_occurrence_turns_all,
                  skip_late_scheduled_turns, expire_stale_drills):
        try:
            sweep()
        except Exception:  # noqa: BLE001
            logger.exception("queue sweep %s failed", sweep.__name__)


def wake_due_runners() -> None:
    """Run every scheduled unpause that has come due, fleet-wide. The runner
    learns it on its next heartbeat reply (its pause fields are read there)."""
    _wake_due_runners(timezone.now())


def register() -> None:
    from apps.common import ticks

    ticks.register("harness.queue_sweeps", queue_sweeps, queue_sweeps_every_s)
    ticks.register("harness.wake_due_runners", wake_due_runners, queue_sweeps_every_s)
