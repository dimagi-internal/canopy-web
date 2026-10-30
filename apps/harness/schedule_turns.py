"""What an `AgentSchedule` does to the turn queue.

canopy-web's own clock (`schedule_clock`) — and any laptop runner still doing
it — evaluates the cron and reports a due slot; `fire_schedule` turns it into an
ordinary Turn. Superseding a still-open occurrence, releasing one that
held the agent past its grace window, retiring slots that could not start in
time, and the review ask an unattended occurrence raises (and a later DONE one
dismisses) all live here — there is no occurrence table; the Turn IS the
occurrence.

`schedule_services.py` is the request-free CRUD over the schedule CONFIG (and its
own `run_schedule_now(user, ...)` resolves and authorizes, then calls the one
here). `enqueue_turn` and `finish_turn` stay in `services.py` and are imported
inside the functions that need them, because `services.py` imports this module.
Split out of `services.py`; import these names from here.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.agents.models import AgentTask
from apps.huddles.models import ProjectCheckin

from . import triggers
from .models import (
    AgentSchedule,
    Turn,
)
from .routing import EXECUTING

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# AgentSchedule — recurring turns. schedule_clock (and any runner) calls fire_schedule;
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


def schedule_initiator(schedule, *, manual: bool = False):
    from . import initiator as who
    via = f"schedule:{schedule.id}" + (":manual" if manual else "")
    return who.system(via=via, accountable=schedule.created_by)


def checkin_of(schedule):
    """The project check-in (apps/huddles) this schedule runs, or None."""
    return ProjectCheckin.objects.filter(schedule_id=schedule.pk).first() if schedule.pk else None


def fire_after_of(schedule) -> dt.datetime:
    """The lower bound a due-slot search starts from — the one anchor every firer
    (the server's own clock, `schedule_clock`, and a runner, via
    `ScheduleOut.fire_after`) passes to `canopy_cron.due_slot(after=...)`.

    `last_slot or created_at`: last_slot is NULL until the first fire, and an
    unbounded backward look would fire a slot that predates the schedule. A
    one-off's anchor is pinned a minute before its instant, so the derived
    cron's earlier-year matches can never be due (schedule_services.one_off_cron).
    """
    after = schedule.last_slot or schedule.created_at
    if schedule.run_once_at:
        after = max(after, schedule.run_once_at - dt.timedelta(minutes=1))
    return after


def _refuse_if_stopped(schedule, *, slot: dt.datetime | None = None) -> None:
    """Raise `triggers.TriggerRefused` when the trigger's creator no longer holds the
    right to it (`triggers.refusal`). Loud, never silent: the refusal is written to
    the workspace event log here, and `alerts_trigger` keeps an alert open on the
    agent until it is fixed. A refused SLOT is consumed (`last_slot`), so the clock
    and every runner stop re-reporting it each tick; the next slot is judged afresh."""
    reason = triggers.refusal(schedule)
    if reason is None:
        return
    if slot is not None and (schedule.last_slot is None or slot > schedule.last_slot):
        schedule.last_slot = slot
        schedule.save(update_fields=["last_slot", "updated_at"])
        _record_refusal(schedule, reason, slot=slot)
    raise triggers.TriggerRefused(schedule, reason)


def _record_refusal(schedule, reason: str, *, slot=None) -> None:
    """One line in the workspace event log (admins read it). Best-effort."""
    try:
        from apps.events.services import record  # noqa: PLC0415 — events reaches the harness

        record([{
            "source": "harness.triggers", "kind": "trigger.refused", "level": "warn",
            "key": f"trigger:{schedule.pk}:refused",
            "summary": f"{schedule.agent.slug}'s trigger {schedule.name!r} did not fire: {reason}"[:500],
            "payload": {"trigger_id": schedule.pk, "slot": slot.isoformat() if slot else None},
        }], workspace=schedule.agent.workspace)
    except Exception:  # noqa: BLE001 — a log line must never fail the fire path
        logger.exception("could not record refusal of trigger %s", schedule.pk)


class OneOffSlotMismatch(Exception):
    """A runner reported a slot for a one-off schedule other than its one instant."""


def fire_schedule(schedule, slot: dt.datetime) -> tuple[Turn | None, bool]:
    """Materialize `slot` as a queued Turn. Supersedes any still-open occurrence
    of the same schedule first — you only ever owe the newest.

    Safe to call concurrently from the server clock and any runner: the
    slot-derived idempotency_key collapses the race inside enqueue_turn.

    A ONE-OFF (run_once_at set) fires only its own instant and then disables
    itself in the same transaction, so it drops out of every runner's sync. Its
    stored cron repeats yearly; this guard is what makes that repetition inert
    even for a runner whose clock or anchor is wrong.

    A PROJECT CHECK-IN's schedule (apps/huddles) does not enqueue a turn here: it
    makes the run and returns (None, created). With a channel it announces the
    run there, and the turn comes later, once the lead confirms and the channel
    has been read; without one the check-in queues the turn itself.
    """
    from .services import enqueue_turn  # services.py imports this module

    _refuse_if_stopped(schedule, slot=slot)
    if schedule.run_once_at and slot != schedule.run_once_at:
        raise OneOffSlotMismatch(
            f"schedule {schedule.id} is a one-off for {schedule.run_once_at.isoformat()}, "
            f"not {slot.isoformat()}"
        )
    if checkin_of(schedule) is not None:
        from apps.huddles import checkins

        _run, created = checkins.announce(schedule, slot)
        return None, created
    key = f"sched:{schedule.id}:{slot.isoformat()}"
    with transaction.atomic():
        if not Turn.keyed(key, Turn.scope_for(agent=schedule.agent)).exists():
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
            requested_cli=schedule.cli,
            # canopy fired it; the schedule's creator is the person accountable
            # for it (spec D3), recorded so a later phase can bound the turn.
            initiator=schedule_initiator(schedule),
        )
        if created and (schedule.last_slot is None or slot > schedule.last_slot):
            schedule.last_slot = slot
            update = ["last_slot", "updated_at"]
            if schedule.run_once_at:
                schedule.enabled = False
                update.append("enabled")
            schedule.save(update_fields=update)
    return turn, created


def run_schedule_now(schedule, clicked_by=None) -> Turn | None:
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

    A project check-in's schedule announces now instead (returns None) —
    pressing Run now never reads the channel; the lead's Confirm does.
    """
    from .services import enqueue_turn  # services.py imports this module

    _refuse_if_stopped(schedule)
    if checkin_of(schedule) is not None:
        from apps.huddles import checkins

        checkins.announce(schedule, timezone.now(), manual=True)
        return None

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
            requested_cli=schedule.cli,
            initiator=schedule_initiator(schedule, manual=True),
            # The schedule's creator is who it is FOR; this is who pressed the
            # button, which is the reason it ran now.
            provenance_extra={"clicked_by": getattr(clicked_by, "email", "") or ""},
        )
    return turn


def release_stale_occurrence_turns(schedule, *, now: dt.datetime | None = None) -> int:
    """Release this schedule's turns that have HELD the agent past grace_minutes.

    This is what keeps a forgotten session from wedging the agent: an executing
    turn occupies a runner, and the runner's heartbeat keeps
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
        # A check-in run's turn is queued when the lead confirms, hours after
        # its slot by design — the slot dates the announcement, not the turn.
        if not t.origin_ref.get("manual") and t.origin_ref.get("kind") not in ("checkin_run", "huddle_group_run")
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
# Schedule nags — an unattended occurrence becomes a real task ask (not a projection)
# ---------------------------------------------------------------------------


def _raise_schedule_nag(schedule, turn: Turn) -> None:
    """A grace-released (unattended) scheduled occurrence becomes a review task.

    Approving it re-runs the schedule's prompt as a fresh turn (its `on_approve`)
    — the generic task action replaces the old bespoke "Run now" nag button;
    declining retires it. Idempotent per released turn: a re-raise of the same
    occurrence collapses on the idempotency key, and a later abandonment gets its
    own row (keyed by the new turn), so a declined nag can legitimately return.

    Honours the schedule's `notify` channel list — the "inbox" channel is what
    materializes this task; a schedule that opts out raises nothing.
    """
    if "inbox" not in (schedule.notify or []):
        return
    from apps.agents import services as agent_services

    again = {"prompt": schedule.prompt, "origin_ref": {"schedule_id": schedule.id, "manual": True,
                                                       "schedule_name": schedule.name}}
    if (turn.origin_ref or {}).get("kind") in ("checkin_run", "huddle_group_run"):
        # Re-run the same check-in run from its stored material — never a new read.
        again = {"prompt": turn.prompt, "origin_ref": {**turn.origin_ref, "manual": True}}
    agent_services.create_tasks(schedule.agent, [{
        "ask_kind": "review",
        "title": f"Scheduled turn unattended: {schedule.name}",
        "ask_body": (
            f"“{schedule.name}” fired but was left unattended past "
            f"{schedule.grace_minutes}m. Approve to run it now, or decline."
        ),
        "origin": Turn.ORIGIN_CANOPY_SCHEDULER,
        "origin_ref": {
            "schedule_id": schedule.id, "turn_id": str(turn.id), "kind": "schedule_nag",
        },
        "on_approve": [{
            "prompt": again["prompt"],
            "origin": Turn.ORIGIN_CANOPY_SCHEDULER,
            "origin_ref": again["origin_ref"],
            "routing": schedule.routing,
        }],
        "idempotency_key": f"sched-nag:{schedule.id}:{turn.id}",
    }])


def resolve_schedule_nags(schedule_id: int) -> int:
    """Decline every open nag for a schedule — a later occurrence finished, so the
    owed attention is discharged. Called from finish_turn on a DONE occurrence."""
    from apps.agents import services as agent_services

    count = 0
    open_nags = (
        AgentTask.objects.filter(ask_closed_at__isnull=True, origin_ref__schedule_id=schedule_id)
        .exclude(ask_kind="")
    )
    for task in open_nags:
        # The read above is unlocked: a person may approve/decline the nag before
        # act() takes its lock. That is a closed nag, not an error — and this runs
        # inside finish_turn for an unrelated turn, which must not fail over it.
        try:
            agent_services.act(task, action="decline", by="system:schedule",
                               actor_workspace_ids=set())
        except agent_services.ClosedAskError:
            continue
        count += 1
    return count
