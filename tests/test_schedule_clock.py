"""canopy-web's own schedule clock (apps/harness/schedule_clock.py).

A schedule fires with no runner: the server tick evaluates the cron from the
same anchor a runner uses and calls the same fire_schedule, so a laptop runner
that still fires the slot collapses onto the server's turn.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.core.cache import cache

from apps.agents.models import Agent
from apps.harness import schedule_clock
from apps.harness import schedule_turns
from apps.harness.models import AgentSchedule, Turn
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db

# Friday 2026-07-10, 09:00 America/New_York == 13:00Z.
SLOT = dt.datetime(2026, 7, 10, 13, tzinfo=dt.UTC)
NOW = SLOT + dt.timedelta(minutes=2)


@pytest.fixture()
def agent():
    return Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())


def _schedule(agent, *, created_at=SLOT - dt.timedelta(days=3), **over):
    fields = {"name": "Weekly manager report", "prompt": "/echo:manager-report",
              "cron": "0 9 * * 5", "timezone": "America/New_York"}
    fields.update(over)
    s = AgentSchedule.objects.create(agent=agent, **fields)
    AgentSchedule.objects.filter(pk=s.pk).update(created_at=created_at)
    s.refresh_from_db()
    return s


def _turns(schedule):
    return Turn.objects.filter(origin_ref__schedule_id=schedule.id)


def test_a_due_schedule_fires_once_from_the_server_tick(agent):
    s = _schedule(agent)

    first = schedule_clock.tick(now=NOW)
    second = schedule_clock.tick(now=NOW + dt.timedelta(minutes=1))

    assert [f["slot"] for f in first["fired"]] == [SLOT.isoformat()]
    assert first["fired"][0]["created"] is True
    assert second["fired"] == []  # last_slot advanced: nothing more is owed
    turn = _turns(s).get()
    assert turn.status == Turn.QUEUED
    assert turn.origin == Turn.ORIGIN_CANOPY_SCHEDULER
    assert turn.idempotency_key == f"sched:{s.id}:{SLOT.isoformat()}"
    s.refresh_from_db()
    assert s.last_slot == SLOT


def test_a_runner_firing_the_same_slot_collapses_onto_the_server_turn(agent):
    s = _schedule(agent)
    schedule_clock.tick(now=NOW)

    turn, created = schedule_turns.fire_schedule(s, SLOT)  # what the runner's POST /fire calls

    assert created is False
    assert _turns(s).count() == 1
    assert turn.id == _turns(s).get().id
    assert turn.status == Turn.QUEUED  # not superseded by its own duplicate


def test_a_server_tick_after_a_runner_fired_the_slot_adds_nothing(agent):
    s = _schedule(agent)
    schedule_turns.fire_schedule(s, SLOT)

    result = schedule_clock.tick(now=NOW)

    assert result["fired"] == []
    assert _turns(s).count() == 1


def test_no_slot_before_the_schedule_existed(agent):
    _schedule(agent, created_at=NOW - dt.timedelta(minutes=1))

    assert schedule_clock.tick(now=NOW)["fired"] == []


def test_a_disabled_schedule_does_not_fire(agent):
    s = _schedule(agent, enabled=False)

    assert schedule_clock.tick(now=NOW)["fired"] == []
    assert not _turns(s).exists()


def test_a_one_off_fires_its_instant_once_and_disables_itself(agent):
    when = SLOT + dt.timedelta(days=2)  # Sunday 09:00 New York
    s = _schedule(agent, cron="0 9 12 7 *", run_once_at=when, name="Reminder")

    assert schedule_clock.tick(now=when - dt.timedelta(minutes=5))["fired"] == []
    fired = schedule_clock.tick(now=when + dt.timedelta(minutes=1))["fired"]

    assert [f["slot"] for f in fired] == [when.isoformat()]
    s.refresh_from_db()
    assert s.enabled is False
    assert schedule_clock.tick(now=when + dt.timedelta(days=366))["fired"] == []
    assert _turns(s).count() == 1


def test_one_bad_schedule_never_stops_the_rest(agent):
    bad = _schedule(agent, name="broken")
    AgentSchedule.objects.filter(pk=bad.pk).update(cron="not a cron")
    good = _schedule(agent, name="good")

    result = schedule_clock.tick(now=NOW)

    assert result["failed"] == [bad.id]
    assert [f["schedule_id"] for f in result["fired"]] == [good.id]


def test_tick_if_due_ticks_once_per_interval_across_processes(agent, monkeypatch):
    cache.delete(schedule_clock.TICK_CACHE_KEY)
    calls = []
    monkeypatch.setattr(schedule_clock, "tick", lambda: calls.append(1) or {"fired": [], "failed": []})

    schedule_clock.tick_if_due(60)
    schedule_clock.tick_if_due(60)

    assert calls == [1]
    cache.delete(schedule_clock.TICK_CACHE_KEY)
