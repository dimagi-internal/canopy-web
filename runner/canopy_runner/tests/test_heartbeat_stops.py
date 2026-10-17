"""A stop rides the heartbeat reply, not only the `cancel` frame.

The frame is a doorbell a reconnecting socket can miss; canopy-web now returns
`cancel_turn_ids` on every heartbeat reply until the turn is finished. Both
runner kinds read it; here it lands in the same set the frame feeds.
"""
from __future__ import annotations

from canopy_runner.cancel import CANCELLED_TURNS
from canopy_runner.main import honour_stops


def test_a_heartbeat_reply_naming_a_turn_marks_it_cancelled():
    CANCELLED_TURNS.discard("t-1")
    try:
        honour_stops({"cancel_turn_ids": ["t-1"], "paused": False})
        assert "t-1" in CANCELLED_TURNS
    finally:
        CANCELLED_TURNS.discard("t-1")


def test_a_failed_or_older_reply_is_not_a_stop():
    before = set(CANCELLED_TURNS)
    honour_stops(None)
    honour_stops({})
    honour_stops({"cancel_turn_ids": None})
    assert CANCELLED_TURNS == before
