"""The off-request clock (apps/common/ticks.py) and what moved onto it.

Periodic work used to ride a runner's heartbeat and claim, so it stopped when no
runner polled. It now runs from the web process's own clock."""
from __future__ import annotations

import datetime as dt

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.common import ticks


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(ticks, "_JOBS", {})
    cache.clear()
    yield


def test_a_due_job_runs_once_per_interval_fleet_wide(clean_registry):
    ran = []
    ticks.register("t.job", lambda: ran.append(1), 60)
    assert ticks.tick() == ["t.job"]
    assert ticks.tick() == []                      # this process: not due yet
    ticks.jobs()["t.job"].last_run = float("-inf")  # another web task, same interval
    assert ticks.tick() == []                      # the cache key says someone ran it
    assert ran == [1]


def test_a_job_runs_on_this_process_clock_when_the_cache_is_down(clean_registry, monkeypatch):
    ran = []
    ticks.register("t.job", lambda: ran.append(1), 60)

    def down(*a, **k):
        raise ConnectionError("redis")

    monkeypatch.setattr(ticks.cache, "add", down)
    assert ticks.tick() == ["t.job"]
    assert ran == [1]


def test_one_failing_job_does_not_stop_the_rest(clean_registry):
    ran = []
    ticks.register("t.boom", lambda: 1 / 0, 60)
    ticks.register("t.ok", lambda: ran.append(1), 60)
    assert ticks.tick() == ["t.ok"]
    assert ran == [1]


def test_an_interval_of_zero_turns_a_job_off(clean_registry, settings):
    settings.SOME_TICK_S = 0
    ticks.register("t.off", lambda: pytest.fail("off"), lambda: settings.SOME_TICK_S)
    assert ticks.tick() == []


def test_every_moved_job_is_registered():
    names = set(ticks.jobs())
    assert {
        "harness.queue_sweeps", "harness.wake_due_runners", "harness.schedule_clock",
        "capabilities.sweep", "slack.channel_ingest", "agents.pat_rotation", "agents.zk_notify",
        "push.due_session_pushes", "canopy_sessions.purge_expired_secrets", "retention.sweep",
        "contacts.hcp_oauth_deliver", "admission.sweep_unrunnable", "admission.expire_runner_notices",
    } <= names


# ---- the harness's jobs -----------------------------------------------------------

@pytest.mark.django_db
def test_the_queue_sweeps_do_not_run_while_the_fleet_is_held(monkeypatch):
    from apps.harness import sweeps
    from apps.harness.models import FleetHold

    ran = []
    for name in ("sweep_expired_leases", "release_stale_occurrence_turns_all",
                 "skip_late_scheduled_turns", "expire_stale_drills"):
        monkeypatch.setattr(sweeps, name, lambda n=name: ran.append(n))
    FleetHold.objects.update_or_create(pk=FleetHold.SINGLETON_PK, defaults={"held": True})
    sweeps.queue_sweeps()
    assert ran == [], "a late slot must not be closed as MISSED under a hold"
    FleetHold.objects.filter(pk=FleetHold.SINGLETON_PK).update(held=False)
    sweeps.queue_sweeps()
    assert ran == ["sweep_expired_leases", "release_stale_occurrence_turns_all",
                   "skip_late_scheduled_turns", "expire_stale_drills"]


@pytest.mark.django_db
def test_one_failing_queue_sweep_does_not_stop_the_others(monkeypatch):
    from apps.harness import sweeps

    ran = []
    monkeypatch.setattr(sweeps, "sweep_expired_leases", lambda: 1 / 0)
    for name in ("release_stale_occurrence_turns_all", "skip_late_scheduled_turns", "expire_stale_drills"):
        monkeypatch.setattr(sweeps, name, lambda n=name: ran.append(n))
    sweeps.queue_sweeps()
    assert len(ran) == 3


@pytest.mark.django_db
def test_a_scheduled_unpause_happens_with_no_runner_beating():
    from django.contrib.auth import get_user_model

    from apps.harness import services, sweeps
    from apps.harness.models import Runner

    owner = get_user_model().objects.create_user("owner-ticks")
    runner = Runner.objects.create(name="parked", kind=Runner.EMDASH, owner=owner, status=Runner.ONLINE)
    services.pause(runner, note="lunch", until=timezone.now() + dt.timedelta(minutes=1))
    Runner.objects.filter(pk=runner.pk).update(unpause_at=timezone.now() - dt.timedelta(seconds=1))
    sweeps.wake_due_runners()
    runner.refresh_from_db()
    assert runner.paused is False


@pytest.mark.django_db
def test_the_heartbeat_is_liveness_only(monkeypatch):
    """None of the moved jobs rides a beat any more."""
    from django.contrib.auth import get_user_model

    from apps.harness import services
    from apps.harness.models import Runner

    def boom(*a, **k):
        raise AssertionError("a periodic job ran on the heartbeat")

    import apps.admission.evaluate as admission
    import apps.push.services as push

    monkeypatch.setattr(services, "wake_due_runners", boom)
    monkeypatch.setattr(admission, "sweep_unrunnable", boom)
    monkeypatch.setattr(push, "send_due_session_pushes", boom)
    owner = get_user_model().objects.create_user("owner-beat")
    runner = Runner.objects.create(name="beat", kind=Runner.EMDASH, owner=owner, status=Runner.ONLINE)
    services.heartbeat(runner, active_turn_ids=[])
    runner.refresh_from_db()
    assert runner.last_heartbeat_at is not None
