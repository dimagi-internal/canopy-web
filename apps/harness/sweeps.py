"""The harness's periodic jobs, run on the off-request clock (apps/common/ticks.py).

They used to ride runner traffic — the queue sweeps every claim poll, the
scheduled unpause every heartbeat — so they stopped whenever no runner was
polling. Registered from HarnessConfig.ready().
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from . import sensitivity
from .claim import sweep_expired_leases
from .closing import close_turn
from .drills import expire_stale_drills
from .models import FleetHold, Turn
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


#: Why `expire_unrun_sensitive_turns` closed a turn: said on the turn, in its note.
UNRUN_SENSITIVE_NOTE = ("a sensitive turn that no runner started within its deadline is closed, "
                        "so its words are not kept waiting")


def sensitive_turn_deadline() -> timedelta:
    return timedelta(seconds=int(getattr(settings, "CANOPY_SENSITIVE_TURN_DEADLINE_SECONDS", 3600)))


def expire_unrun_sensitive_turns(now=None) -> int:
    """Close every sensitive turn still QUEUED or CLAIMED past its deadline
    (`CANOPY_SENSITIVE_TURN_DEADLINE_SECONDS` after it could first run:
    `_unrun_past_deadline`) through
    `close_turn`, which wipes its prompt — the person's words, in clear, held only so
    a runner could run it. Runs whatever the fleet hold says: a held turn's words
    are no safer for waiting. A turn that started (RUNNING, NEEDS_HUMAN) is live and
    ends the ordinary way. Returns how many it closed."""
    now = now or timezone.now()
    stale = (Turn.objects.filter(status__in=[Turn.QUEUED, Turn.CLAIMED],
                                 created_at__lt=now - sensitive_turn_deadline())
             .select_related("chat_session", "canopy_session", "parent_session", "parent_turn__chat_session",
                             "parent_turn__parent_session"))
    closed = 0
    for turn in stale.iterator(chunk_size=200):
        if not sensitivity.is_sensitive(turn):
            continue
        if not _unrun_past_deadline(turn, now):
            continue
        if close_turn(turn, Turn.MISSED, UNRUN_SENSITIVE_NOTE,
                      from_states=[Turn.QUEUED, Turn.CLAIMED], now=now,
                      event={"reason": "sensitive_deadline"}, auto_debug=False):
            closed += 1
    return closed


def _unrun_past_deadline(turn, now) -> bool:
    """Whether a queued sensitive turn has waited out its deadline since it could
    first RUN. A follow-up queued behind a turn of its conversation still running
    is not stale, however long that turn takes; once the conversation is free, its
    clock starts from when the last turn before it that ran ended (`finished_at`),
    or from when it was asked if that is later."""
    conversation = turn.chat_session_id or turn.canopy_session_id
    if not conversation:
        return True
    others = Turn.objects.filter(sensitivity.turns_of_q(conversation)).exclude(pk=turn.pk)
    if others.filter(status__in=[Turn.RUNNING, Turn.NEEDS_HUMAN]).exists():
        return False
    # Only a turn that RAN held the conversation; one closed unrun (by this sweep) did not.
    last_end = others.filter(status__in=list(Turn.TERMINAL), started_at__isnull=False,
                             created_at__lte=turn.created_at).aggregate(m=Max("finished_at"))["m"]
    runnable_since = max(turn.created_at, last_end) if last_end else turn.created_at
    return runnable_since < now - sensitive_turn_deadline()


def wake_due_runners() -> None:
    """Run every scheduled unpause that has come due, fleet-wide. The runner
    learns it on its next heartbeat reply (its pause fields are read there)."""
    _wake_due_runners(timezone.now())


def register() -> None:
    from apps.common import ticks

    ticks.register("harness.queue_sweeps", queue_sweeps, queue_sweeps_every_s)
    ticks.register("harness.wake_due_runners", wake_due_runners, queue_sweeps_every_s)
    ticks.register("harness.sensitive_turn_deadline", expire_unrun_sensitive_turns, 60)
