"""Scheduled-turn trigger — cron slots → scheduled turns.

Tests the DECISION (is a slot due, is fire called with the right slot), not the HTTP
and not the cron math: DST/cron correctness is canopy_cron's suite, and duplicating it
here would just re-test croniter through a second door.
"""
import datetime as dt

from canopy_runner import schedules

# Fridays at 09:00 America/New_York — the shape of the motivating schedule
# ("Echo's weekly manager report, Fridays 9am ET").
FRIDAY_9AM = "0 9 * * 5"
TZ = "America/New_York"

# 2026-07-10 and 2026-07-17 are Fridays; ET is UTC-4 in July, so the 09:00 ET slot
# lands at 13:00 UTC.
SLOT_JUL_10 = dt.datetime(2026, 7, 10, 13, 0, tzinfo=dt.UTC)


def _schedule(**over) -> dict:
    base = {
        "id": 1,
        "agent_slug": "echo",
        "name": "weekly manager report",
        "prompt": "/echo:turn",
        "cron": FRIDAY_9AM,
        "timezone": TZ,
        "enabled": True,
        "routing": "prefer_local",
        "grace_minutes": 60,
        "notify": [],
        "last_slot": None,
        "fire_after": "2026-07-08T12:00:00+00:00",  # Wed — schedule created midweek
    }
    return {**base, **over}


class FakeClient:
    def __init__(self, schedules_=None, fire_error=None):
        self.schedules = list(schedules_ or [])
        self.fired = []
        self.fire_error = fire_error

    def sync_schedules(self, runner_id):
        return self.schedules

    def fire_schedule(self, schedule_id, runner_id, slot):
        if self.fire_error is not None:
            raise self.fire_error
        self.fired.append({"schedule_id": schedule_id, "runner_id": runner_id, "slot": slot})
        return {"id": f"turn-{schedule_id}"}


def test_nothing_due_fires_nothing():
    # Thursday: the Friday slot hasn't happened yet.
    client = FakeClient([_schedule()])
    res = schedules.check_schedules(
        client, "r-1", now=dt.datetime(2026, 7, 9, 12, 0, tzinfo=dt.UTC)
    )
    assert client.fired == []
    assert res == {"fired": [], "failed": []}


def test_due_slot_fires_once_with_the_slot():
    client = FakeClient([_schedule()])
    res = schedules.check_schedules(
        client, "r-1", now=dt.datetime(2026, 7, 10, 14, 0, tzinfo=dt.UTC)  # Fri, after 9am ET
    )
    assert len(client.fired) == 1
    assert client.fired[0]["schedule_id"] == 1
    assert dt.datetime.fromisoformat(client.fired[0]["slot"]) == SLOT_JUL_10
    assert res["fired"][0]["turn_id"] == "turn-1"


def test_fire_after_is_the_anchor_not_last_slot():
    """The trap: last_slot is NULL until the first fire. Anchoring on it (or on None)
    looks backward unbounded and fires a schedule for a slot that PREDATES it."""
    # Created Wed 2026-07-08; the previous Friday (Jul 3) is before that anchor.
    client = FakeClient([_schedule(last_slot=None, fire_after="2026-07-08T12:00:00+00:00")])
    res = schedules.check_schedules(
        client, "r-1", now=dt.datetime(2026, 7, 8, 18, 0, tzinfo=dt.UTC)  # still Wed
    )
    assert client.fired == []  # would have fired Jul 3 had it anchored on last_slot/None
    assert res["fired"] == []


def test_no_backfill_three_weeks_offline_owes_one_slot():
    """due_slot returns AT MOST one slot: a laptop offline for weeks owes the NEWEST
    occurrence only, not one turn per missed Friday."""
    client = FakeClient([_schedule(last_slot="2026-06-26T13:00:00+00:00",
                                   fire_after="2026-06-26T13:00:00+00:00")])
    schedules.check_schedules(
        client, "r-1", now=dt.datetime(2026, 7, 17, 14, 0, tzinfo=dt.UTC)  # 3 Fridays later
    )
    assert len(client.fired) == 1
    assert dt.datetime.fromisoformat(client.fired[0]["slot"]) == \
        dt.datetime(2026, 7, 17, 13, 0, tzinfo=dt.UTC)  # the newest, not Jul 3


def test_disabled_schedule_is_skipped():
    client = FakeClient([_schedule(enabled=False)])
    schedules.check_schedules(client, "r-1", now=dt.datetime(2026, 7, 10, 14, 0, tzinfo=dt.UTC))
    assert client.fired == []


def test_paused_agent_does_not_fire():
    """A paused runner/agent burns no tokens — firing would queue turns that all
    execute the moment it resumes."""
    client = FakeClient([_schedule()])
    schedules.check_schedules(client, "r-1", now=dt.datetime(2026, 7, 10, 14, 0, tzinfo=dt.UTC),
                              paused={"echo"})
    assert client.fired == []


def test_one_failing_schedule_does_not_stop_the_others():
    class HalfBroken(FakeClient):
        def fire_schedule(self, schedule_id, runner_id, slot):
            if schedule_id == 1:
                raise RuntimeError("500 from server")
            return super().fire_schedule(schedule_id, runner_id, slot)

    client = HalfBroken([_schedule(id=1), _schedule(id=2, agent_slug="eva")])
    res = schedules.check_schedules(
        client, "r-1", now=dt.datetime(2026, 7, 10, 14, 0, tzinfo=dt.UTC)
    )
    assert res["failed"] == [1]
    assert [f["schedule_id"] for f in res["fired"]] == [2]  # the healthy one still fired


def test_bad_cron_is_skipped_not_raised():
    client = FakeClient([_schedule(cron="not a cron"), _schedule(id=2)])
    res = schedules.check_schedules(
        client, "r-1", now=dt.datetime(2026, 7, 10, 14, 0, tzinfo=dt.UTC)
    )
    assert res["failed"] == [1]
    assert [f["schedule_id"] for f in res["fired"]] == [2]


def test_missing_fire_after_never_fires_unbounded():
    """No anchor means we cannot know the schedule predates the slot — skip, never
    fall back to due_slot(after=None)."""
    client = FakeClient([_schedule(fire_after=None)])
    res = schedules.check_schedules(
        client, "r-1", now=dt.datetime(2026, 7, 10, 14, 0, tzinfo=dt.UTC)
    )
    assert client.fired == []
    assert res["failed"] == [1]
