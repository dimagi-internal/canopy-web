"""A hook for what should follow a box's first event of the day, owned by the log's API.

`POST /api/events/` tells the harness (via the callbacks registered here) about each event a box's
runner token recorded that is the first of its (source, key) today, so a credential problem also
reaches the people who can fix it. The log itself stays inert (`tests/test_events_emits_nothing.py`):
this is the route calling a function the higher layer handed it, not the log promoting a fault.
"""
from __future__ import annotations

_callbacks: dict[str, object] = {}


def register(name: str, fn) -> None:
    """`fn(runner, item)` is called, in the request, for each such event. Re-registering a name replaces it."""
    _callbacks[name] = fn


def first_today(runner, item: dict) -> None:
    for fn in list(_callbacks.values()):
        fn(runner, item)
