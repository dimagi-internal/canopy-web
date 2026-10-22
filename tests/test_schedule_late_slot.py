"""skip_late_scheduled_turns — a slot too late to start is skipped, not run."""
from __future__ import annotations

import datetime as dt

import pytest

from apps.agents.models import Agent
from apps.harness import schedule_turns
from apps.harness.models import AgentSchedule, Turn
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db

SLOT = dt.datetime(2026, 9, 23, 14, tzinfo=dt.UTC)
WINDOW = dt.timedelta(minutes=schedule_turns.LATE_SLOT_WINDOW_MINUTES)


@pytest.fixture()
def schedule():
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=a_workspace())
    return AgentSchedule.objects.create(
        agent=agent, name="Chief of staff turn", prompt="/eva:chief-of-staff",
        cron="0 10 * * *", timezone="America/New_York",
    )


def test_a_slot_past_the_window_is_skipped_as_missed(schedule):
    """The 2026-09-23 case: laptop reopened hours after the slot."""
    turn, _ = schedule_turns.fire_schedule(schedule, SLOT)

    assert schedule_turns.skip_late_scheduled_turns(now=SLOT + dt.timedelta(hours=5)) == 1

    turn.refresh_from_db()
    assert turn.status == Turn.MISSED
    assert "skipped" in turn.result_note and "300m late" in turn.result_note


def test_a_slot_inside_the_window_still_runs(schedule):
    turn, _ = schedule_turns.fire_schedule(schedule, SLOT)

    assert schedule_turns.skip_late_scheduled_turns(now=SLOT + WINDOW - dt.timedelta(minutes=1)) == 0

    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


def test_always_run_schedules_are_never_skipped(schedule):
    schedule.always_run = True
    schedule.save()
    turn, _ = schedule_turns.fire_schedule(schedule, SLOT)

    assert schedule_turns.skip_late_scheduled_turns(now=SLOT + dt.timedelta(days=2)) == 0

    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


def test_a_manual_run_now_is_never_skipped(schedule):
    turn = schedule_turns.run_schedule_now(schedule)

    assert schedule_turns.skip_late_scheduled_turns(
        now=turn.created_at + dt.timedelta(days=2)) == 0

    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


def test_skipping_advances_nothing_the_next_slot_still_fires(schedule):
    schedule_turns.fire_schedule(schedule, SLOT)
    schedule_turns.skip_late_scheduled_turns(now=SLOT + dt.timedelta(hours=5))

    nxt, created = schedule_turns.fire_schedule(schedule, SLOT + dt.timedelta(days=1))

    assert created is True and nxt.status == Turn.QUEUED
    schedule.refresh_from_db()
    assert schedule.last_slot == SLOT + dt.timedelta(days=1)
