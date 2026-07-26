"""fire_schedule / release_stale_occurrence_turns — supersede, idempotency, unwedging."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import AgentSchedule, Runner, RunnerAssignment, Turn
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db

SLOT_A = dt.datetime(2026, 7, 10, 13, tzinfo=dt.UTC)
SLOT_B = dt.datetime(2026, 7, 17, 13, tzinfo=dt.UTC)


@pytest.fixture()
def agent():
    return Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())


@pytest.fixture()
def schedule(agent):
    return AgentSchedule.objects.create(
        agent=agent, name="Weekly manager report", prompt="/echo:manager-report",
        cron="0 9 * * 5", timezone="America/New_York",
    )


def test_fire_creates_a_cron_turn_and_advances_last_slot(schedule):
    turn, created = services.fire_schedule(schedule, SLOT_A)

    assert created is True
    assert turn.origin == Turn.ORIGIN_CRON
    assert turn.status == Turn.QUEUED
    assert turn.prompt == "/echo:manager-report"
    assert turn.origin_ref == {"schedule_id": schedule.id, "slot": SLOT_A.isoformat()}
    assert turn.idempotency_key == f"sched:{schedule.id}:{SLOT_A.isoformat()}"
    schedule.refresh_from_db()
    assert schedule.last_slot == SLOT_A


def test_fire_is_idempotent_two_runners_one_turn(schedule):
    """Both macOS-account runners may fire the same slot. Exactly one turn."""
    first, created_1 = services.fire_schedule(schedule, SLOT_A)
    second, created_2 = services.fire_schedule(schedule, SLOT_A)

    assert created_1 is True
    assert created_2 is False
    assert first.id == second.id
    assert Turn.objects.filter(origin=Turn.ORIGIN_CRON).count() == 1


def test_fire_supersedes_the_prior_unfinished_turn(schedule):
    old, _ = services.fire_schedule(schedule, SLOT_A)

    services.fire_schedule(schedule, SLOT_B)

    old.refresh_from_db()
    assert old.status == Turn.MISSED
    assert "superseded" in old.result_note
    assert Turn.objects.filter(status=Turn.QUEUED).count() == 1  # only the newest is owed


def test_fire_does_not_touch_a_finished_turn(schedule):
    done, _ = services.fire_schedule(schedule, SLOT_A)
    # A queued turn is not finishable (only the scheduler may retire one, as
    # MISSED) — so drive it through running to reach a genuine DONE.
    Turn.objects.filter(pk=done.pk).update(status=Turn.RUNNING)
    done.refresh_from_db()
    services.finish_turn(done, status=Turn.DONE)
    assert done.status == Turn.DONE  # setup actually reached terminal

    services.fire_schedule(schedule, SLOT_B)

    done.refresh_from_db()
    assert done.status == Turn.DONE  # not rewritten to missed


def test_release_stale_unwedges_the_agent(schedule, agent):
    """The one_executing_turn_per_agent finding: an abandoned session must not
    block the agent's next turn forever."""
    turn, _ = services.fire_schedule(schedule, SLOT_A)
    # claimed_at is the grace anchor — grace_minutes bounds how long a turn may
    # HOLD the agent, and holding starts when it is claimed (created_at would
    # measure owed time, a different quantity).
    Turn.objects.filter(pk=turn.pk).update(
        status=Turn.RUNNING, claimed_at=timezone.now() - dt.timedelta(minutes=200)
    )

    released = services.release_stale_occurrence_turns(schedule)

    assert released == 1
    turn.refresh_from_db()
    assert turn.status == Turn.MISSED
    # Proof it is unwedged: a new executing turn is now insertable.
    Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_BOARD, idempotency_key="board-1", status=Turn.RUNNING
    )


def test_release_spares_a_turn_inside_its_grace(schedule):
    turn, _ = services.fire_schedule(schedule, SLOT_A)
    Turn.objects.filter(pk=turn.pk).update(
        status=Turn.RUNNING, claimed_at=timezone.now() - dt.timedelta(minutes=5)
    )

    assert services.release_stale_occurrence_turns(schedule) == 0
    turn.refresh_from_db()
    assert turn.status == Turn.RUNNING


def test_release_spares_a_long_queued_turn(schedule):
    """A queued turn holds NOTHING (the executing index does not cover it), so
    releasing it cannot unwedge anything — it would only destroy work still
    owed. Laptop offline Friday→Monday must not retire Friday's slot."""
    turn, _ = services.fire_schedule(schedule, SLOT_A)
    Turn.objects.filter(pk=turn.pk).update(
        created_at=timezone.now() - dt.timedelta(days=3)
    )

    assert services.release_stale_occurrence_turns(schedule) == 0
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


def test_release_does_not_abort_a_freshly_claimed_long_queued_turn(schedule):
    """The created_at anchor's real bite: a turn queued longer than grace was
    born past-grace and got aborted on its first sweep after being claimed —
    killing live human work in the function meant to protect it."""
    turn, _ = services.fire_schedule(schedule, SLOT_A)
    Turn.objects.filter(pk=turn.pk).update(
        status=Turn.RUNNING,
        created_at=timezone.now() - dt.timedelta(days=3),
        claimed_at=timezone.now(),
    )

    assert services.release_stale_occurrence_turns(schedule) == 0
    turn.refresh_from_db()
    assert turn.status == Turn.RUNNING


def test_release_all_unwedges_on_the_claim_tick(schedule, agent):
    """Fleet-wide release runs on claim, so a wedged agent is unblocked by the
    very same claim — a weekly schedule's fire tick is 10,080m apart and could
    never do this."""
    wedged, _ = services.fire_schedule(schedule, SLOT_A)
    Turn.objects.filter(pk=wedged.pk).update(
        status=Turn.RUNNING, claimed_at=timezone.now() - dt.timedelta(minutes=200)
    )
    queued = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_BOARD, idempotency_key="board-1"
    )
    runner = Runner.objects.create(
        name="mac-a", kind=Runner.EMDASH, host="mac-a", status=Runner.ONLINE,
        capabilities={"agents": ["echo"]}, last_heartbeat_at=timezone.now(),
        paired_by=a_member(),  # a runner's tenant derives from its pairer
    )
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)

    claimed = services.claim_next_turn(runner)

    wedged.refresh_from_db()
    assert wedged.status == Turn.MISSED
    assert claimed is not None and claimed.pk == queued.pk


def test_latest_occurrence_turn_returns_the_newest_whatever_its_status(schedule):
    older, _ = services.fire_schedule(schedule, SLOT_A)
    Turn.objects.filter(pk=older.pk).update(status=Turn.RUNNING)
    older.refresh_from_db()
    services.finish_turn(older, status=Turn.DONE)
    assert older.status == Turn.DONE
    newer, _ = services.fire_schedule(schedule, SLOT_B)

    assert services.latest_occurrence_turn(schedule).pk == newer.pk


def test_latest_occurrence_turn_returns_a_terminal_newest_not_an_open_older(schedule):
    """The newest occurrence wins even when it is the *terminal* one.

    The sibling test above leaves the newest QUEUED, so a hypothetical
    `status__in=NON_TERMINAL` filter would still return it and pass — it pins
    recency but not the "whatever its status" half of the contract. Here the DONE
    turn IS the newest and an *older* turn is still open, so such a filter would
    skip past the answer and hand back the stale open one.

    That's the nag's whole correctness condition: `needs_you` asks "what is the
    latest occurrence, and is it unfinished?" If a status filter could reach back
    to an older open turn, completing this week's report would leave last week's
    abandoned one nagging forever.
    """
    older, _ = services.fire_schedule(schedule, SLOT_A)
    newer, _ = services.fire_schedule(schedule, SLOT_B)  # supersedes `older` → MISSED
    Turn.objects.filter(pk=newer.pk).update(status=Turn.RUNNING)
    newer.refresh_from_db()
    services.finish_turn(newer, status=Turn.DONE)
    # Re-open the older turn directly: firing always supersedes, so this ordering
    # (terminal newest, open older) has no natural path — but `latest_occurrence_turn`
    # is a pure read, and this is the state a status filter would mis-answer.
    Turn.objects.filter(pk=older.pk).update(status=Turn.QUEUED)

    latest = services.latest_occurrence_turn(schedule)

    assert latest is not None, "a status filter would drop the terminal newest entirely"
    assert latest.pk == newer.pk, "returned the stale open older turn, not the newest"
    assert latest.status == Turn.DONE


def test_latest_occurrence_turn_sees_a_manual_run(schedule):
    """"Run now" writes origin=manual; an origin=cron-only lookup would never
    see it, so the nag it launched could never be cleared by completing it."""
    services.fire_schedule(schedule, SLOT_A)

    manual = services.run_schedule_now(schedule)

    assert services.latest_occurrence_turn(schedule).pk == manual.pk


def test_supersede_retires_an_open_manual_run(schedule):
    """You only owe the newest attempt, however it was launched."""
    manual = services.run_schedule_now(schedule)

    services.fire_schedule(schedule, SLOT_B)

    manual.refresh_from_db()
    assert manual.status == Turn.MISSED


def test_run_now_does_not_advance_the_cadence(schedule):
    """A manual run leaves `last_slot` alone, so the next real slot still fires
    on time. It DOES supersede an open occurrence (see the test below) — what it
    never does is consume a slot."""
    manual = services.run_schedule_now(schedule)

    assert manual.origin == Turn.ORIGIN_MANUAL
    assert manual.idempotency_key.startswith(f"sched:{schedule.id}:manual:")
    schedule.refresh_from_db()
    assert schedule.last_slot is None  # a manual run does not consume a slot


def test_run_now_supersedes_an_open_slot_so_the_work_never_runs_twice(schedule):
    """Run now is the spec's own designed remediation for a missed slot — so it
    must retire the slot it is remediating.

    Laptop offline Friday -> Monday the slot turn is QUEUED and nagging -> the
    human clicks Run now. Without supersede, finishing the manual turn clears the
    nag (it is the newest occurrence) while the slot turn sits QUEUED and still
    owed — it gets claimed later and generates the report a SECOND time.
    """
    slot_turn, _ = services.fire_schedule(schedule, SLOT_A)
    assert slot_turn.status == Turn.QUEUED

    manual = services.run_schedule_now(schedule)

    slot_turn.refresh_from_db()
    assert slot_turn.status == Turn.MISSED  # retired: you only owe the newest
    assert services.latest_occurrence_turn(schedule).pk == manual.pk
    schedule.refresh_from_db()
    assert schedule.last_slot == SLOT_A  # cadence untouched — the next slot still fires
