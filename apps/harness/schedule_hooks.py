"""What a schedule may be besides "enqueue a turn for its agent", without naming it.

A project check-in's schedule (`apps/huddles`) makes a run and announces it instead of
enqueuing a turn. The harness owns schedules and firing; huddles owns check-ins, so it
registers one announcer from `AppConfig.ready()` and the harness asks it. With none
registered every schedule enqueues a turn.

An announcer has three methods:
  `owns(schedule)` -> bool          this schedule is not an ordinary agent turn;
  `announce(schedule, slot, manual=False)` -> (run, created)   make/announce its run;
  `run_for(schedule, slot)` -> run | None   the run that slot made, if any.
"""
from __future__ import annotations

_announcer = None


def register(announcer) -> None:
    global _announcer
    _announcer = announcer


def owns(schedule) -> bool:
    return bool(schedule.pk) and _announcer is not None and _announcer.owns(schedule)


def announce(schedule, slot, *, manual: bool = False):
    return _announcer.announce(schedule, slot, manual=manual)


def announced_run(schedule, slot):
    """The run `slot` of `schedule` made, or None (also None when no announcer is registered)."""
    return _announcer.run_for(schedule, slot) if _announcer is not None else None
