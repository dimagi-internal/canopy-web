"""What a schedule may be besides "enqueue a turn for its agent", without naming it.

A project check-in's schedule (`apps/huddles`) makes a run and announces it instead of
enqueuing a turn; so does a learning target's (`apps/brain`, a brain run). The harness owns
schedules and firing; the app that owns the thing a schedule fires registers one announcer
from `AppConfig.ready()` and the harness asks each. A schedule is owned by at most one; with
none owning it a schedule enqueues a turn.

An announcer has three methods:
  `owns(schedule)` -> bool          this schedule is not an ordinary agent turn;
  `announce(schedule, slot, manual=False)` -> (run, created)   make/announce its run (run may be None);
  `run_for(schedule, slot)` -> run | None   the run that slot made, if any.
"""
from __future__ import annotations

_announcers: list = []


def register(announcer) -> None:
    """Add an announcer; registering one of the same type again replaces it (a reload stays one)."""
    _announcers[:] = [a for a in _announcers if type(a) is not type(announcer)] + [announcer]


def _owner(schedule):
    if not schedule.pk:
        return None
    return next((a for a in _announcers if a.owns(schedule)), None)


def owns(schedule) -> bool:
    return _owner(schedule) is not None


def announce(schedule, slot, *, manual: bool = False):
    return _owner(schedule).announce(schedule, slot, manual=manual)


def announced_run(schedule, slot):
    """The run `slot` of `schedule` made, or None (also None when no announcer owns it)."""
    owner = _owner(schedule)
    return owner.run_for(schedule, slot) if owner is not None else None
