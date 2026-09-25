"""One-off schedules — `run_once_at` fires exactly once, then disables itself.

The stored cron is derived and repeats yearly; these tests pin the three things
that make that repetition inert: the fire_after anchor, the self-disable on
fire, and the server refusing any other slot.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from canopy_cron import due_slot
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import schedule_services as ss
from apps.harness import services
from apps.harness.models import AgentSchedule, Turn
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership

pytestmark = pytest.mark.django_db

DENVER = ZoneInfo("America/Denver")


def _next_week_9am_denver() -> dt.datetime:
    day = (timezone.now() + dt.timedelta(days=7)).astimezone(DENVER).date()
    return dt.datetime(day.year, day.month, day.day, 9, 0, tzinfo=DENVER)


@pytest.fixture()
def owner(default_workspace):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    wsvc.ensure_member(default_workspace, user, WorkspaceMembership.OWNER)
    return user


@pytest.fixture()
def agent(default_workspace):
    return Agent.objects.create(slug="ace", name="ACE", workspace=default_workspace)


def _one_off(owner, when, **over):
    fields = {"name": "Reminder", "prompt": "/ace:remind", "timezone": "America/Denver",
              "run_once_at": when}
    fields.update(over)
    return ss.create_schedule(owner, "ace", fields)


def test_one_off_derives_its_cron_in_the_schedules_timezone(owner, agent):
    when = _next_week_9am_denver()
    s = _one_off(owner, when)

    assert s.run_once_at == when
    assert s.cron == f"0 9 {when.day} {when.month} *"
    row = ss.serialize_schedule(s)
    assert row["next_runs"] == [when]  # one fire, not three yearly ones


def test_naive_run_once_at_is_wall_clock_time_where_the_schedule_lives(owner, agent):
    when = _next_week_9am_denver()
    s = _one_off(owner, when.replace(tzinfo=None))

    assert s.run_once_at == when


def test_a_one_off_in_the_past_is_refused(owner, agent):
    with pytest.raises(ss.InvalidSchedule, match="not in the future"):
        _one_off(owner, timezone.now() - dt.timedelta(minutes=5))


@pytest.mark.parametrize("cron", [None, "0 9 * * 1"])
def test_exactly_one_of_cron_and_run_once_at(owner, agent, cron):
    fields = {"name": "x", "prompt": "/x", "cron": cron}
    if cron:
        fields["run_once_at"] = _next_week_9am_denver()
    with pytest.raises(ss.InvalidSchedule):
        ss.create_schedule(owner, "ace", fields)


def test_runner_finds_the_slot_due_only_at_its_instant_and_never_a_prior_year(owner, agent):
    """The runner's own math: due_slot(cron, tz, after=fire_after, now=...). The
    derived cron also matches the same date LAST year — an unpinned anchor on a
    one-off created more than a year ahead would fire that."""
    when = _next_week_9am_denver()
    s = _one_off(owner, when)
    # Simulate a schedule created long ago, so created_at alone would not guard it.
    AgentSchedule.objects.filter(pk=s.pk).update(created_at=when - dt.timedelta(days=800))
    s.refresh_from_db()
    row = ss.serialize_schedule(s)

    def due(now):
        return due_slot(row["cron"], row["timezone"], after=row["fire_after"], now=now)

    assert due(when - dt.timedelta(minutes=1)) is None
    assert due(when + dt.timedelta(minutes=1)) == when


def test_firing_disables_it_so_it_drops_out_of_every_runner_sync(owner, agent):
    when = _next_week_9am_denver()
    s = _one_off(owner, when)

    turn, created = services.fire_schedule(s, when)

    assert created and turn.status == Turn.QUEUED
    s.refresh_from_db()
    assert s.enabled is False
    assert ss.serialize_schedule(s)["next_runs"] == []


def test_any_other_slot_is_refused(owner, agent):
    when = _next_week_9am_denver()
    s = _one_off(owner, when)

    with pytest.raises(services.OneOffSlotMismatch):
        services.fire_schedule(s, when.replace(year=when.year + 1))
    assert not Turn.objects.filter(origin_ref__schedule_id=s.id).exists()


def test_a_late_one_off_still_runs(owner, agent):
    """Skipping a late recurring slot defers it to the next one; a one-off has no
    next one, so skipping would silently drop it."""
    when = _next_week_9am_denver()
    s = _one_off(owner, when)
    turn, _ = services.fire_schedule(s, when)

    assert services.skip_late_scheduled_turns(now=when + dt.timedelta(hours=6)) == 0
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


def test_re_arming_a_fired_one_off_enables_it_again(owner, agent):
    when = _next_week_9am_denver()
    s = _one_off(owner, when)
    services.fire_schedule(s, when)

    later = when + dt.timedelta(days=1)
    s = ss.update_schedule(owner, "ace", s.id, {"run_once_at": later})

    assert s.enabled is True and s.run_once_at == later


def test_setting_a_cron_makes_it_recurring_again(owner, agent):
    s = _one_off(owner, _next_week_9am_denver())

    s = ss.update_schedule(owner, "ace", s.id, {"cron": "0 9 * * 1"})

    assert s.run_once_at is None and s.cron == "0 9 * * 1"
    assert len(ss.serialize_schedule(s)["next_runs"]) == 3


def test_changing_timezone_keeps_the_instant_and_re_derives_the_cron(owner, agent):
    when = _next_week_9am_denver()
    s = _one_off(owner, when)

    s = ss.update_schedule(owner, "ace", s.id, {"timezone": "America/New_York"})

    assert s.run_once_at == when
    assert s.cron == f"0 11 {when.day} {when.month} *"


def test_week_view_shows_the_one_fire_only(owner, agent, default_workspace):
    when = _next_week_9am_denver()
    _one_off(owner, when)

    rows = ss.week_schedules({default_workspace.slug}, when - dt.timedelta(days=1))

    assert [r["fires"] for r in rows] == [[when]]


def test_rest_create_one_off_and_422_on_past(owner, agent):
    c = Client()
    c.force_login(owner)
    when = _next_week_9am_denver()
    ok = c.post("/api/agents/ace/schedules/",
                {"name": "Reminder", "prompt": "/ace:remind", "timezone": "America/Denver",
                 "run_once_at": when.isoformat()},
                content_type="application/json")
    assert ok.status_code == 201, ok.content
    assert ok.json()["run_once_at"] is not None
    assert len(ok.json()["next_runs"]) == 1

    past = c.post("/api/agents/ace/schedules/",
                  {"name": "Old", "prompt": "/x",
                   "run_once_at": (timezone.now() - dt.timedelta(hours=1)).isoformat()},
                  content_type="application/json")
    assert past.status_code == 422

    both = c.post("/api/agents/ace/schedules/",
                  {"name": "Both", "prompt": "/x", "cron": "0 9 * * 1",
                   "run_once_at": when.isoformat()},
                  content_type="application/json")
    assert both.status_code == 422


def test_mcp_create_takes_a_naive_local_time_and_needs_no_cron(owner, agent):
    """The reminder path: an agent says "9am Monday, Denver" and nothing else."""
    from apps.mcp.tools.schedules import _create_sync

    when = _next_week_9am_denver()
    row = _create_sync(owner.id, "ace", "Reminder", "/ace:remind", "", "America/Denver",
                       True, "prefer_local", 120, ["inbox"],
                       when.replace(tzinfo=None).isoformat())

    assert row["run_once_at"] == when
    assert row["next_runs"] == [when]
