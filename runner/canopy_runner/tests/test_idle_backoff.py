"""#647: an idle runner backs off its poll, but only where that is safe.

On labs, runner polling was ~98% of all ALB traffic on an idle Saturday and
/claim answered 204 every time. The loop now stretches its wait once it has been
quiet for a while, and only while the WS wake channel is up, because that channel
delivers every doorbell (enqueue, viewer attach, session stop, inbox) at once.
"""
from __future__ import annotations

import types

from canopy_runner import activity, inbox_due
from canopy_runner.main import IDLE_POLL_CAP_SECONDS, make_control_handler, next_wait
from canopy_runner.wake import WakeListener


def _cfg(**kw):
    base = {"poll_seconds": 5, "idle_poll_seconds": 30, "idle_after_ticks": 12}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_a_busy_or_freshly_idle_runner_keeps_the_normal_cadence():
    assert next_wait(_cfg(), quiet_streak=0, wake_connected=True) == 5
    assert next_wait(_cfg(), quiet_streak=11, wake_connected=True) == 5


def test_a_long_quiet_runner_backs_off():
    assert next_wait(_cfg(), quiet_streak=12, wake_connected=True) == 30
    assert next_wait(_cfg(), quiet_streak=500, wake_connected=True) == 30


def test_no_backoff_without_the_wake_channel():
    """With the channel down the poll is the only way work arrives."""
    assert next_wait(_cfg(), quiet_streak=500, wake_connected=False) == 5


def test_the_backoff_stays_inside_the_heartbeat_window():
    """The server reads a runner as stale 90s after its last heartbeat."""
    assert next_wait(_cfg(idle_poll_seconds=600), quiet_streak=500,
                     wake_connected=True) == IDLE_POLL_CAP_SECONDS
    assert IDLE_POLL_CAP_SECONDS < 90


def test_zero_or_a_short_idle_interval_disables_it():
    assert next_wait(_cfg(idle_poll_seconds=0), quiet_streak=500, wake_connected=True) == 5
    assert next_wait(_cfg(idle_poll_seconds=3), quiet_streak=500, wake_connected=True) == 5


def test_activity_is_read_once_per_tick():
    activity.take()
    assert activity.take() is False
    activity.note()
    activity.note()
    assert activity.take() is True
    assert activity.take() is False


def test_inbox_doorbell_ends_a_backoff_wait():
    """The inbox doorbell used to only mark the mailbox due, which was fine at a
    5s tick but would sit out a whole idle wait."""
    waker = WakeListener("http://x", "t", "r1")
    cfg = types.SimpleNamespace(cdp_port=9222, runner_id="r1", base_url="http://x",
                                token="t", state_path=None)
    on_control = make_control_handler(cfg, waker)
    on_control({"type": "check_inbox", "mailbox": "eva@dimagi-ai.com"})
    assert waker.event.is_set()
    inbox_due.take_pending()  # leave no doorbell behind for other tests


def test_the_listener_starts_disconnected():
    assert WakeListener("http://x", "t", "r1").connected is False
