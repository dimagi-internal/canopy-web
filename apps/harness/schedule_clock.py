"""canopy-web fires due schedules itself: an asyncio task in every web process.

A schedule used to fire only when a laptop runner evaluated its cron and POSTed
the due slot (`runner/canopy_runner/canopy_runner/schedules.py`), so a
workspace whose agents run only on fleet cloud boxes — or whose laptop was shut
— never fired a schedule at all. This clock evaluates every enabled schedule on
the server, with the same slot math (`canopy_cron.due_slot`) from the same
anchor (`schedule_turns.fire_after_of`), and calls the same
`schedule_turns.fire_schedule`. A laptop runner that still fires is harmless:
both reach `sched:<id>:<slot>`, the slot-derived idempotency key, so the second
fire of a slot collapses onto the first turn.

It runs on the off-request clock (apps/common/ticks.py, registered from
HarnessConfig.ready): `tick_if_due` every CANOPY_SCHEDULE_TICK_S (default 60 s).
Its cache key keeps N web tasks to about one tick per interval (and this
process ticks anyway if the cache is down — a duplicate tick is collapsed by
the same key). CANOPY_SCHEDULE_TICK_S <= 0 turns it off.

The rules the runner holds hold here too: never fire without an anchor (a slot
older than the schedule is never owed), at most one slot per schedule per tick
(no backfill — you only owe the newest), and one schedule's failure never stops
the rest. A fired slot is an ordinary queued turn: which box claims it, and
whether it is skipped as too late (`skip_late_scheduled_turns`), is decided on
the claim as before.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import AgentSchedule

logger = logging.getLogger(__name__)

TICK_CACHE_KEY = "harness:schedule-clock:tick"


def interval_s() -> float:
    return float(getattr(settings, "CANOPY_SCHEDULE_TICK_S", 60) or 0)


def tick(*, now: dt.datetime | None = None) -> dict:
    """Fire every enabled schedule whose slot has come due. Returns
    {"fired": [{schedule_id, agent, slot, turn_id, created}], "failed": [ids]}."""
    from canopy_cron import due_slot

    from .schedule_turns import OneOffSlotMismatch, fire_after_of, fire_schedule

    now = now or timezone.now()
    fired: list[dict] = []
    failed: list[int] = []
    for schedule in AgentSchedule.objects.filter(enabled=True).select_related("agent"):
        try:
            slot = due_slot(schedule.cron, schedule.timezone,
                            after=fire_after_of(schedule), now=now)
        except ValueError as exc:
            logger.warning("schedule %s (%s): %s — skipping", schedule.id, schedule.agent.slug, exc)
            failed.append(schedule.id)
            continue
        if slot is None:
            continue
        try:
            turn, created = fire_schedule(schedule, slot)
        except OneOffSlotMismatch as exc:
            logger.warning("schedule %s: %s", schedule.id, exc)
            failed.append(schedule.id)
            continue
        except Exception:  # noqa: BLE001 — one bad schedule never stops the others
            logger.exception("schedule %s (%s): fire for slot %s failed",
                             schedule.id, schedule.agent.slug, slot.isoformat())
            failed.append(schedule.id)
            continue
        if created:
            logger.info("schedule %s (%s) '%s': slot %s due — fired by canopy-web",
                        schedule.id, schedule.agent.slug, schedule.name, slot.isoformat())
        fired.append({"schedule_id": schedule.id, "agent": schedule.agent.slug,
                      "slot": slot.isoformat(), "turn_id": str(turn.id) if turn else "",
                      "created": created})
    return {"fired": fired, "failed": failed}


def tick_if_due(interval: float) -> dict | None:
    """`tick`, at most once per interval across every web process (the cache
    lock); this process runs it anyway if the cache is down."""
    try:
        if not cache.add(TICK_CACHE_KEY, timezone.now().isoformat(), max(1, int(interval) - 1)):
            return None
    except Exception:  # noqa: BLE001
        logger.warning("schedule clock: cache unavailable; ticking on this process's clock")
    return tick()


def run() -> None:
    """The registered job: `tick_if_due` at the configured interval."""
    tick_if_due(interval_s())
