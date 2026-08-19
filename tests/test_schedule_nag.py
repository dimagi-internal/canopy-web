"""The nag: a grace-released (unattended) scheduled occurrence raises a real Item.

This is not a projection — release_stale_occurrence_turns raises a review Item whose
`implement` re-runs the schedule; a later finished occurrence dismisses it via
finish_turn. Firing alone does NOT nag: only holding the agent past grace does.
"""
from __future__ import annotations

import datetime as dt

import pytest

from apps.agents.models import Agent
from apps.harness import services as hsvc
from apps.harness.models import AgentSchedule, Item, Turn

pytestmark = pytest.mark.django_db

SLOT = dt.datetime(2026, 7, 17, 13, tzinfo=dt.UTC)


def _claimed_long_ago(turn: Turn, *, minutes: int) -> Turn:
    """Drive a fired (QUEUED) turn into EXECUTING with a claimed_at far enough in
    the past that release_stale_occurrence_turns treats it as unattended. Release is
    scoped to EXECUTING and anchored on claimed_at (see services.release_stale...)."""
    claimed = dt.datetime(2026, 7, 17, 13, tzinfo=dt.UTC) - dt.timedelta(minutes=minutes)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING, claimed_at=claimed)
    turn.refresh_from_db()
    return turn


def _open_nags(agent) -> list[Item]:
    return list(
        agent.items.filter(state=Item.OPEN, origin_ref__kind="schedule_nag").order_by("created_at")
    )


@pytest.fixture()
def agent(default_workspace):
    return Agent.objects.create(slug="eva", name="Eva", workspace=default_workspace)


@pytest.fixture()
def schedule(agent):
    return AgentSchedule.objects.create(
        agent=agent, name="Goal review", prompt="/eva:goal-review",
        cron="0 9 1 * *", timezone="America/New_York", grace_minutes=120,
    )


def test_never_fired_schedule_does_not_nag(agent, schedule):
    assert _open_nags(agent) == []


def test_firing_alone_does_not_nag(agent, schedule):
    """A freshly fired (queued) slot owes nothing yet — the nag is about HOLDING
    the agent past grace, not about a slot merely existing."""
    hsvc.fire_schedule(schedule, SLOT)
    assert _open_nags(agent) == []


def test_grace_released_occurrence_raises_a_review_nag(agent, schedule):
    turn, _ = hsvc.fire_schedule(schedule, SLOT)
    _claimed_long_ago(turn, minutes=200)  # past the 120m grace

    released = hsvc.release_stale_occurrence_turns(schedule, now=SLOT)

    assert released == 1
    nags = _open_nags(agent)
    assert len(nags) == 1
    assert nags[0].kind == Item.REVIEW
    assert nags[0].title == "Scheduled turn unattended: Goal review"
    # implement re-runs the schedule's prompt (self-dispatch) — the generic
    # replacement for the old "Run now" button.
    assert nags[0].dispatch[0]["prompt"] == "/eva:goal-review"


def test_a_finished_later_occurrence_clears_the_nag(agent, schedule):
    turn, _ = hsvc.fire_schedule(schedule, SLOT)
    _claimed_long_ago(turn, minutes=200)
    hsvc.release_stale_occurrence_turns(schedule, now=SLOT)
    assert len(_open_nags(agent)) == 1

    # A later occurrence completes -> the owed attention is discharged.
    later, _ = hsvc.fire_schedule(schedule, SLOT + dt.timedelta(days=31))
    Turn.objects.filter(pk=later.pk).update(status=Turn.RUNNING)
    later.refresh_from_db()
    hsvc.finish_turn(later, status=Turn.DONE)

    assert _open_nags(agent) == []


def test_implementing_the_nag_re_runs_the_schedule(agent, schedule):
    turn, _ = hsvc.fire_schedule(schedule, SLOT)
    _claimed_long_ago(turn, minutes=200)
    hsvc.release_stale_occurrence_turns(schedule, now=SLOT)
    nag = _open_nags(agent)[0]

    item, turns = hsvc.decide_item(
        nag, decision=Item.IMPLEMENT, comment="", by="jj@dimagi.com", actor_workspace_slugs=set(),
    )

    assert item.state == Item.DECIDED
    assert len(turns) == 1
    # Stamped: a nag item's brief is machine-authored like any other card prompt.
    assert turns[0].prompt.startswith("/eva:goal-review")
    assert _open_nags(agent) == []  # decided -> out of the inbox


def test_a_schedule_that_opts_out_of_the_inbox_channel_does_not_nag(agent, schedule):
    schedule.notify = ["carrier_pigeon"]  # no "inbox" channel
    schedule.save()
    turn, _ = hsvc.fire_schedule(schedule, SLOT)
    _claimed_long_ago(turn, minutes=200)

    hsvc.release_stale_occurrence_turns(schedule, now=SLOT)

    assert _open_nags(agent) == []
