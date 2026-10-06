"""What an `AgentSchedule` does to the turn queue.

The runner evaluates the cron and reports a due slot; `fire_schedule` turns it
into an ordinary Turn. Superseding a still-open occurrence, releasing one that
held the agent past its grace window, retiring slots that could not start in
time, and the review ask an unattended occurrence raises (and a later DONE one
dismisses) all live here — there is no occurrence table; the Turn IS the
occurrence.

`schedule_services.py` is the request-free CRUD over the schedule CONFIG (and its
own `run_schedule_now(user, ...)` resolves and authorizes, then calls the one
here). `enqueue_turn` and `finish_turn` stay in `services.py` and are imported
inside the functions that need them, because `services.py` imports this module.
Split out of `services.py`, which still re-exports every name here.
"""
from __future__ import annotations

import datetime as dt
import uuid

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    AgentSchedule,
    Turn,
)
from .routing import EXECUTING

# --------------------------------------------------------------------------------------
# AgentSchedule — recurring turns. The runner evaluates the cron and calls fire_schedule;
# the server materializes a normal Turn. See models.AgentSchedule.
# --------------------------------------------------------------------------------------

def _occurrences(schedule):
    """This schedule's turns — scheduled AND manual. Occurrence-based, not
    origin-based: a "Run now" turn is an attempt at the same work, so it must
    participate in latest/supersede/release exactly as a fired slot does."""
    return Turn.objects.filter(
        agent_id=schedule.agent_id, origin_ref__schedule_id=schedule.id
    )


def latest_occurrence_turn(schedule) -> Turn | None:
    """The newest turn this schedule produced — scheduled or manual run-now —
    whatever its status."""
    return _occurrences(schedule).order_by("-created_at").first()


def supersede_open_turns(schedule, *, reason: str) -> int:
    """Terminate this schedule's non-terminal turns as MISSED. Supersede and
    grace-release are the same operation at two timescales."""
    from .services import finish_turn  # services.py imports this module

    open_turns = _occurrences(schedule).filter(status__in=list(Turn.NON_TERMINAL))
    count = 0
    for turn in open_turns:
        finish_turn(turn, status=Turn.MISSED, result_note=reason, allow_queued=True)
        count += 1
    return count


def _schedule_initiator(schedule, *, manual: bool = False):
    from . import initiator as who
    via = f"schedule:{schedule.id}" + (":manual" if manual else "")
    return who.system(via=via, accountable=schedule.created_by)


class OneOffSlotMismatch(Exception):
    """A runner reported a slot for a one-off schedule other than its one instant."""


def fire_schedule(schedule, slot: dt.datetime) -> tuple[Turn, bool]:
    """Materialize `slot` as a queued Turn. Supersedes any still-open occurrence
    of the same schedule first — you only ever owe the newest.

    Safe to call concurrently from both macOS-account runners: the slot-derived
    idempotency_key collapses the race inside enqueue_turn.

    A ONE-OFF (run_once_at set) fires only its own instant and then disables
    itself in the same transaction, so it drops out of every runner's sync. Its
    stored cron repeats yearly; this guard is what makes that repetition inert
    even for a runner whose clock or anchor is wrong.
    """
    from .services import enqueue_turn  # services.py imports this module

    if schedule.run_once_at and slot != schedule.run_once_at:
        raise OneOffSlotMismatch(
            f"schedule {schedule.id} is a one-off for {schedule.run_once_at.isoformat()}, "
            f"not {slot.isoformat()}"
        )
    key = f"sched:{schedule.id}:{slot.isoformat()}"
    with transaction.atomic():
        if not Turn.objects.filter(idempotency_key=key).exists():
            supersede_open_turns(schedule, reason=f"superseded by slot {slot.isoformat()}")
        turn, created = enqueue_turn(
            agent=schedule.agent,
            origin=Turn.ORIGIN_CANOPY_SCHEDULER,
            idempotency_key=key,
            prompt=schedule.prompt,
            # `schedule_name` is carried for the emdash session NAME. Without it the
            # runner names a cron turn after the prompt's slash command, so every
            # schedule an agent has reads `c-turn-<disc>` in the sidebar; with it
            # they read `c-weekly-manager-report`, which is what the human called it.
            origin_ref={"schedule_id": schedule.id, "slot": slot.isoformat(),
                        "schedule_name": schedule.name},
            routing=schedule.routing,
            # canopy fired it; the schedule's creator is the person accountable
            # for it (spec D3), recorded so a later phase can bound the turn.
            initiator=_schedule_initiator(schedule),
        )
        if created and (schedule.last_slot is None or slot > schedule.last_slot):
            schedule.last_slot = slot
            update = ["last_slot", "updated_at"]
            if schedule.run_once_at:
                schedule.enabled = False
                update.append("enabled")
            schedule.save(update_fields=update)
    return turn, created


def run_schedule_now(schedule, clicked_by=None) -> Turn:
    """Manual off-cycle trigger. Supersedes any still-open occurrence first,
    exactly as fire_schedule does — you only ever owe the newest, however it was
    launched. Run now is the designed remediation for an unfinished slot, so it
    must retire the slot it remediates; otherwise finishing the manual turn
    clears the nag (it is the newest occurrence) while the slot turn sits queued
    and still owed, and the work runs a second time when it is claimed later.

    origin=canopy_scheduler with origin_ref["manual"]=True and a uuid-suffixed
    key, so an ad-hoc run never collides with a real slot, and last_slot is
    untouched — the CADENCE is unaffected (the next real slot still fires on
    time). The manual flag lives in origin_ref rather than in origin because WHO
    fired it is not a different SOURCE: both route as scheduler work, which is
    the point of naming the source.
    """
    from .services import enqueue_turn  # services.py imports this module

    with transaction.atomic():
        supersede_open_turns(schedule, reason="superseded by a manual run")
        turn, _ = enqueue_turn(
            agent=schedule.agent,
            origin=Turn.ORIGIN_CANOPY_SCHEDULER,
            idempotency_key=f"sched:{schedule.id}:manual:{uuid.uuid4()}",
            prompt=schedule.prompt,
            origin_ref={"schedule_id": schedule.id, "manual": True,
                        "schedule_name": schedule.name},
            routing=schedule.routing,
            initiator=_schedule_initiator(schedule, manual=True),
            # The schedule's creator is who it is FOR; this is who pressed the
            # button, which is the reason it ran now.
            provenance_extra={"clicked_by": getattr(clicked_by, "email", "") or ""},
        )
    return turn


def release_stale_occurrence_turns(schedule, *, now: dt.datetime | None = None) -> int:
    """Release this schedule's turns that have HELD the agent past grace_minutes.

    This is what keeps a forgotten session from wedging the agent: an executing
    turn holds one_executing_turn_per_agent, and the runner's heartbeat keeps
    renewing its lease for as long as the emdash session is open, so the ordinary
    lease sweep never rescues it.

    Scoped to EXECUTING (not NON_TERMINAL) and anchored on claimed_at (not
    created_at) because both are statements about *holding*, which is what
    grace_minutes bounds:
      - a QUEUED turn holds nothing (the index does not cover it), so releasing
        it could not unwedge anything. Retiring a stale queued occurrence is
        supersede_open_turns' job (a newer slot) or skip_late_scheduled_turns'
        (a slot too late to be worth running).
      - created_at measures *owed* time, so a turn queued longer than grace would
        be born past-grace and get aborted on its first sweep after being claimed
        — killing live human work in the function meant to protect it.
    claimed_at is non-null for every EXECUTING turn: claim_next_turn writes it,
    and claiming is the only route into those states.
    """
    from .services import finish_turn  # services.py imports this module

    now = now or timezone.now()
    cutoff = now - dt.timedelta(minutes=schedule.grace_minutes)
    stale = _occurrences(schedule).filter(status__in=EXECUTING, claimed_at__lt=cutoff)
    count = 0
    for turn in stale:
        finish_turn(
            turn, status=Turn.MISSED,
            result_note=f"released after {schedule.grace_minutes}m unattended",
        )
        _raise_schedule_nag(schedule, turn)
        count += 1
    return count


#: How late past its slot a scheduled turn may still be claimed. Past it, the slot
#: is skipped as MISSED rather than run, unless its schedule is `always_run`.
LATE_SLOT_WINDOW_MINUTES = 30


def skip_late_scheduled_turns(*, now: dt.datetime | None = None) -> int:
    """Retire QUEUED slot turns that could not start within LATE_SLOT_WINDOW_MINUTES
    of their slot. Runs lazily on the claim tick, like the grace release.

    WHY. No-backfill already collapses a long outage to one slot per schedule, but
    it still fires that one however stale it is: on 2026-09-23 a laptop reopened at
    19:21Z fired all five schedules at once, for slots from 14:00Z to 18:00Z. A
    morning briefing or a chief-of-staff sweep five hours late is noise, not work
    owed. The next slot fires on time as usual.

    Anchored on the SLOT, not created_at, so it covers both ways of being late:
    fired late (runner offline) and fired on time but queued behind a busy agent.
    Manual "Run now" turns carry no slot and are never touched; `always_run`
    schedules opt out. MISSED with no nag: skipping is the designed outcome, not
    an unattended turn that needs a human.
    """
    from .services import finish_turn  # services.py imports this module

    now = now or timezone.now()
    cutoff = now - dt.timedelta(minutes=LATE_SLOT_WINDOW_MINUTES)
    # Manual is filtered in Python, not with .exclude(origin_ref__manual=True): a
    # slot turn has no "manual" key, the negated JSON lookup evaluates to NULL for
    # it, and SQL would silently exclude every row this function exists to find.
    queued = [
        t for t in Turn.objects.filter(
            status=Turn.QUEUED, origin=Turn.ORIGIN_CANOPY_SCHEDULER
        ).only("id", "origin_ref", "status")
        if not t.origin_ref.get("manual")
    ]
    if not queued:
        return 0
    # A one-off is exempt like always_run: skipping it would not defer it to "the
    # next slot" — there is none — it would silently drop the only occurrence.
    always = set(
        AgentSchedule.objects.filter(
            Q(always_run=True) | Q(run_once_at__isnull=False),
            id__in={t.origin_ref.get("schedule_id") for t in queued},
        ).values_list("id", flat=True)
    )
    count = 0
    for turn in queued:
        if turn.origin_ref.get("schedule_id") in always:
            continue
        try:
            slot = dt.datetime.fromisoformat(turn.origin_ref.get("slot", ""))
        except (TypeError, ValueError):
            continue
        if slot.tzinfo is None:
            slot = slot.replace(tzinfo=dt.UTC)
        if slot >= cutoff:
            continue
        late = int((now - slot).total_seconds() // 60)
        finish_turn(
            turn, status=Turn.MISSED, allow_queued=True,
            result_note=f"skipped: not started within {LATE_SLOT_WINDOW_MINUTES}m of "
            f"its slot ({late}m late); the next slot fires on schedule",
        )
        count += 1
    return count


def release_stale_occurrence_turns_all(*, now: dt.datetime | None = None) -> int:
    """Fleet-wide release, run lazily on the claim tick (see claim_next_turn).

    Release belongs here, not on the fire tick: fire already supersedes
    everything release would touch, and a weekly schedule's fire tick is 10,080
    minutes apart — it could never honour a 120-minute grace between
    occurrences. On claim, a release unblocks the very same claim.

    The scan is a handful of rows: the executing-turn index caps this at ~one
    turn per agent.
    """
    now = now or timezone.now()
    schedule_ids = {
        turn.origin_ref.get("schedule_id")
        for turn in Turn.objects.filter(status__in=EXECUTING).only("origin_ref")
    }
    schedule_ids.discard(None)
    if not schedule_ids:
        return 0
    return sum(
        release_stale_occurrence_turns(schedule, now=now)
        for schedule in AgentSchedule.objects.filter(id__in=schedule_ids)
    )


# ---------------------------------------------------------------------------
# Schedule nags — an unattended occurrence becomes a real Item (not a projection)
# ---------------------------------------------------------------------------


def _raise_schedule_nag(schedule, turn: Turn) -> None:
    """A grace-released (unattended) scheduled occurrence becomes a review Item.

    Its `implement` re-runs the schedule's prompt as a fresh turn — the generic
    Item action replaces the old bespoke "Run now" nag button. `skip`/`defer`
    (or `dismiss`) retire it. Idempotent per released turn: a re-raise of the same
    occurrence collapses on the idempotency key, and a later abandonment gets its
    own row (keyed by the new turn), so a dismissed nag can legitimately return.

    Honours the schedule's `notify` channel list — the "inbox" channel is what
    materializes this Item; a schedule that opts out raises nothing.
    """
    if "inbox" not in (schedule.notify or []):
        return
    from apps.agents import services as agent_services

    agent_services.raise_asks(agent=schedule.agent, payloads=[{
        "ask_kind": "review",
        "title": f"Scheduled turn unattended: {schedule.name}",
        "ask_body": (
            f"“{schedule.name}” fired but was left unattended past "
            f"{schedule.grace_minutes}m. Implement to run it now, or skip."
        ),
        "origin": Turn.ORIGIN_CANOPY_SCHEDULER,
        "origin_ref": {
            "schedule_id": schedule.id, "turn_id": str(turn.id), "kind": "schedule_nag",
        },
        "dispatch": [{
            "prompt": schedule.prompt,
            "origin": Turn.ORIGIN_CANOPY_SCHEDULER,
            "origin_ref": {"schedule_id": schedule.id, "manual": True,
                           "schedule_name": schedule.name},
            "routing": schedule.routing,
        }],
        "idempotency_key": f"sched-nag:{schedule.id}:{turn.id}",
    }])


def resolve_schedule_nags(schedule_id: int) -> int:
    """Dismiss every open nag for a schedule — a later occurrence finished, so the
    owed attention is discharged. Called from finish_turn on a DONE occurrence."""
    from apps.agents import services as agent_services
    from apps.agents.models import AgentTask

    count = 0
    open_nags = (
        AgentTask.objects.filter(decided_at__isnull=True, origin_ref__schedule_id=schedule_id)
        .exclude(ask_kind="")
    )
    for task in open_nags:
        agent_services.dismiss_ask(task, by="system:schedule")
        count += 1
    return count
