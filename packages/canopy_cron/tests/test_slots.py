"""Slot math for AgentSchedule. No DB, no Django — pure functions."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from canopy_cron import (
    due_slot,
    next_slots,
    slots_between,
    validate_cron,
    validate_timezone,
)

FRIDAYS_9AM = "0 9 * * 5"
NY = "America/New_York"


def _utc(y, m, d, h, mi=0) -> dt.datetime:
    return dt.datetime(y, m, d, h, mi, tzinfo=dt.UTC)


def test_validate_cron_accepts_and_rejects():
    assert validate_cron(FRIDAYS_9AM) == FRIDAYS_9AM
    with pytest.raises(ValueError):
        validate_cron("not a cron")
    with pytest.raises(ValueError):
        validate_cron("")


def test_validate_timezone_accepts_and_rejects():
    assert validate_timezone(NY) == NY
    with pytest.raises(ValueError):
        validate_timezone("Mars/Olympus_Mons")


def test_due_slot_none_before_first_fire():
    """A schedule anchored at creation owes nothing until its first real slot.

    The creation time is a Wednesday and the next Friday 9am ET has not happened. Note the
    anchor is what makes this None: `due_slot` looks BACKWARD, and the Friday before
    the creation time is a slot itself, so an unanchored call would legitimately return it (see
    test_due_slot_unanchored_returns_the_previous_slot). Anchoring on the creation
    time is what stops a slot from before the schedule existed from firing.
    """
    created = _utc(2026, 7, 15, 11)
    assert due_slot(FRIDAYS_9AM, NY, after=created, now=_utc(2026, 7, 15, 12)) is None


def test_due_slot_unanchored_returns_the_previous_slot():
    """`after=None` has no lower bound, so the most recent past slot IS due.

    Pins the contract rather than leaving it accidental: callers that must not fire
    a pre-creation slot are responsible for passing an anchor. AgentSchedule.last_slot
    is nullable and starts None, so this path is reachable in production — see the
    task-3 report.
    """
    slot = due_slot(FRIDAYS_9AM, NY, after=None, now=_utc(2026, 7, 15, 12))
    assert slot == _utc(2026, 7, 10, 13)  # the previous Friday, 09:00 ET under EDT


def test_due_slot_returns_the_slot_once_passed():
    # The Friday 09:00 ET slot == 13:00 UTC (EDT, UTC-4).
    slot = due_slot(FRIDAYS_9AM, NY, after=None, now=_utc(2026, 7, 17, 14))
    assert slot == _utc(2026, 7, 17, 13)


def test_due_slot_respects_after_no_refire():
    slot = _utc(2026, 7, 17, 13)
    assert due_slot(FRIDAYS_9AM, NY, after=slot, now=_utc(2026, 7, 17, 14)) is None


def test_due_slot_never_backfills():
    """Three weeks offline yields ONE slot — the newest — not three."""
    slot = due_slot(FRIDAYS_9AM, NY, after=_utc(2026, 6, 26, 13), now=_utc(2026, 7, 17, 14))
    assert slot == _utc(2026, 7, 17, 13)  # newest, not the two Fridays before it


def test_due_slot_dst_holds_local_9am():
    """9am ET stays 9am across the DST shift: EDT=13:00Z, EST=14:00Z."""
    edt = due_slot(FRIDAYS_9AM, NY, after=None, now=_utc(2026, 10, 30, 20))
    assert edt == _utc(2026, 10, 30, 13)
    # This Friday is after the US shift back to EST (UTC-5).
    est = due_slot(FRIDAYS_9AM, NY, after=_utc(2026, 10, 30, 13), now=_utc(2026, 11, 6, 20))
    assert est == _utc(2026, 11, 6, 14)


def test_next_slots_previews_three():
    out = next_slots(FRIDAYS_9AM, NY, now=_utc(2026, 7, 15, 12), count=3)
    assert out == [_utc(2026, 7, 17, 13), _utc(2026, 7, 24, 13), _utc(2026, 7, 31, 13)]


def test_next_slots_dst_holds_local_9am_across_the_shift():
    """The preview must cross the DST boundary the way firing does.

    `next_slots` does its OWN zone conversion — it does not share a code path with
    `due_slot` — so `test_due_slot_dst_holds_local_9am` above pins nothing here, and
    every other `next_slots` case sits entirely inside EDT.

    The US shifts back to EST on a Sunday in early November, so a window opened the Monday
    before spans it: 9am ET is 13:00Z under EDT (UTC-4) and 14:00Z under EST
    (UTC-5). The same cron expression must therefore yield two *different* UTC
    instants either side of the shift. Evaluating in UTC would emit a constant
    09:00Z; anchoring the whole window to the start offset would emit a constant
    13:00Z. Both are wrong, and both are invisible to a single-offset window.

    A bug here mislabels rather than misfires — but the preview is the only thing
    that makes a hand-typed cron trustworthy, so a wrong "Next" is a wrong schedule.
    """
    # `now` is the Monday before the shift; the window covers Fridays either side.
    out = next_slots(FRIDAYS_9AM, NY, now=_utc(2026, 10, 26, 12), count=3)

    assert out == [
        _utc(2026, 10, 30, 13),  # Fri 09:00 EDT (UTC-4) — before the shift
        _utc(2026, 11, 6, 14),   # Fri 09:00 EST (UTC-5) — after it
        _utc(2026, 11, 13, 14),  # Fri 09:00 EST
    ]
    # The point of the window: the UTC instant moves precisely because the local
    # wall clock does not.
    assert out[1] - out[0] == dt.timedelta(days=7, hours=1)
    assert out[2] - out[1] == dt.timedelta(days=7)


def test_next_slots_are_always_9am_local_whatever_the_offset():
    """Restates the contract in the units the user actually set it in.

    The assertion above is in UTC and so encodes the offsets by hand; this one
    reads the wall clock back through the zone, and would fail on any zone-handling
    regression without anyone having to recompute an offset table.
    """
    zone = ZoneInfo(NY)
    out = next_slots(FRIDAYS_9AM, NY, now=_utc(2026, 10, 26, 12), count=6)

    local = [slot.astimezone(zone) for slot in out]
    assert {(t.hour, t.minute) for t in local} == {(9, 0)}
    assert {t.strftime("%A") for t in local} == {"Friday"}
    # the window genuinely straddles the shift, else this proves nothing
    assert {t.tzname() for t in local} == {"EDT", "EST"}


def test_slots_between_daily_yields_seven_in_a_week():
    # A daily 9am UTC cron over a 7-day UTC window → exactly 7 fires.
    start = dt.datetime(2026, 7, 13, 0, 0, tzinfo=dt.UTC)   # Mon 00:00
    end = start + dt.timedelta(days=7)
    fires = slots_between("0 9 * * *", "UTC", start=start, end=end)
    assert len(fires) == 7
    assert fires[0] == dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.UTC)
    assert fires[-1] == dt.datetime(2026, 7, 19, 9, 0, tzinfo=dt.UTC)


def test_slots_between_empty_window():
    # A weekly Friday cron over a Mon–Thu window → no fires.
    start = dt.datetime(2026, 7, 13, 0, 0, tzinfo=dt.UTC)   # Mon
    end = dt.datetime(2026, 7, 17, 0, 0, tzinfo=dt.UTC)     # Fri 00:00 (before 9am Fri)
    assert slots_between("0 9 * * 5", "UTC", start=start, end=end) == []


def test_slots_between_is_half_open_inclusive_start_exclusive_end():
    # A fire EXACTLY at start is included; a fire EXACTLY at end is not.
    start = dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.UTC)   # a fire lands here
    end = dt.datetime(2026, 7, 14, 9, 0, tzinfo=dt.UTC)     # and here
    fires = slots_between("0 9 * * *", "UTC", start=start, end=end)
    assert fires == [dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.UTC)]  # start in, end out


def test_slots_between_dst_holds_local_9am_across_the_shift():
    # US Eastern falls back on the first Sunday of the window's November. A daily 9am-ET cron stays 9am LOCAL:
    # 13:00Z before the shift (EDT, UTC-4), 14:00Z after (EST, UTC-5).
    start = dt.datetime(2026, 10, 30, 0, 0, tzinfo=dt.UTC)
    end = start + dt.timedelta(days=7)
    fires = slots_between("0 9 * * *", "America/New_York", start=start, end=end)
    # Convert each to the ET wall clock and assert it reads 09:00 every day.
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    assert all(f.astimezone(et).hour == 9 and f.astimezone(et).minute == 0 for f in fires)
    # And the offset genuinely changed mid-window (proves it isn't fixed-offset).
    # The first Friday after the shift (14:00Z) is >= end (Nov 6 00:00Z), so the
    # half-open window correctly excludes it.
    # Nov 2 is an in-window post-shift EST fire, which is what proves the offset moved.
    assert dt.datetime(2026, 10, 30, 13, 0, tzinfo=dt.UTC) in fires   # EDT (UTC-4)
    assert dt.datetime(2026, 11, 2, 14, 0, tzinfo=dt.UTC) in fires    # EST (UTC-5)
