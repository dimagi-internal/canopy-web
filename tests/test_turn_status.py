"""The shared turn-status projection — the one answer every channel renders.

Drives all eleven states through `derive`, which is pure, so none of this needs
a fleet, a socket or a Slack workspace. The channel-specific renderers are
tested where they live (`test_slack.py` for the mrkdwn line, the kit's own
tests for the chat panel); what is pinned here is the STATE, because that is
the thing three channels now agree on.
"""
from __future__ import annotations

import datetime as dt
import types

import pytest

from apps.harness import turn_status as ts
from apps.harness.models import Turn


class _Runner:
    """Just enough runner for the projection: it reads names and reachability."""

    def __init__(self, name="jj-mbp", reachable=True, last_heartbeat_at=None, rid="r1"):
        self.name = name
        self.is_reachable = reachable
        self.last_heartbeat_at = last_heartbeat_at
        self.id = rid


def _turn(status=Turn.QUEUED, *, claimed=None, pinned=None, agent_slug=None):
    """A Turn stand-in. `derive` only reads attributes, never the DB, which is
    the point of keeping it pure."""
    t = types.SimpleNamespace(
        status=status,
        chat_session_id=None,
        chat_session=None,
        agent_id=1 if agent_slug else None,
        agent=types.SimpleNamespace(slug=agent_slug) if agent_slug else None,
        claimed_by_id=1 if claimed else None,
        claimed_by=claimed,
        pinned_runner_id=1 if pinned else None,
        pinned_runner=pinned,
    )
    return t


def _reach(kind, runners=()):
    return types.SimpleNamespace(kind=kind, runners=list(runners))


# -- the three queued states: same wait, different thing to do about it --------

def test_queued_with_a_live_runner_is_picking_up():
    r = _Runner("jj-mbp")
    st = ts.derive(_turn(Turn.QUEUED), reach=_reach("live", [r]))
    assert st.state == ts.PICKING_UP
    assert st.runners == ("jj-mbp",)
    assert st.pending and not st.settled
    # Nothing for a person to do — this one resolves itself.
    assert not st.stuck


def test_queued_pinned_runner_names_the_pin_not_the_cascade():
    pinned = _Runner("cloud-1")
    other = _Runner("jj-mbp")
    st = ts.derive(_turn(Turn.QUEUED, pinned=pinned), reach=_reach("live", [other]))
    assert st.state == ts.PICKING_UP
    assert st.runners == ("cloud-1",)


def test_queued_with_offline_runners_is_waiting_on_one():
    st = ts.derive(_turn(Turn.QUEUED), reach=_reach("offline", [_Runner("jj-mbp")]))
    assert st.state == ts.WAITING_RUNNER
    assert st.runners == ("jj-mbp",)
    # The distinction that matters: a person has to act.
    assert st.stuck


def test_queued_with_no_covering_runner_is_unrouted():
    st = ts.derive(_turn(Turn.QUEUED, agent_slug="hal"), reach=_reach("config", []))
    assert st.state == ts.UNROUTED
    assert st.agent_slug == "hal"
    assert st.stuck


def test_queued_with_no_reach_computed_falls_to_unrouted_not_a_crash():
    """`reach=None` means nobody asked. Claiming a live runner on no evidence
    is the fail-open shape this projection exists to remove, so the honest
    answer is the pessimistic one."""
    st = ts.derive(_turn(Turn.QUEUED), reach=None)
    assert st.state == ts.UNROUTED


# -- claimed, and the one state the turn itself gets wrong ---------------------

def test_claimed_is_working_and_names_its_holder():
    st = ts.derive(_turn(Turn.RUNNING, claimed=_Runner("jj-mbp")))
    assert st.state == ts.WORKING
    assert st.claimed_by == "jj-mbp"
    assert not st.stuck


def test_a_dead_holder_outranks_what_the_turn_says():
    """The turn still reads RUNNING — a dead runner cannot say otherwise. This
    is the whole reason the projection is not just `Turn.status`."""
    seen = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc)
    gone = _Runner("jj-mbp", reachable=False, last_heartbeat_at=seen)
    st = ts.derive(_turn(Turn.RUNNING, claimed=gone))
    assert st.state == ts.PAUSED
    assert st.last_seen_at == seen
    assert st.stuck


def test_needs_human_is_blocked():
    st = ts.derive(_turn(Turn.NEEDS_HUMAN, claimed=_Runner("jj-mbp")))
    assert st.state == ts.BLOCKED
    assert st.stuck


def test_needs_human_on_a_dead_runner_reports_the_dead_runner():
    """Both are true; the actionable one wins. Answering a dialog on a laptop
    that is shut does nothing."""
    gone = _Runner("jj-mbp", reachable=False)
    st = ts.derive(_turn(Turn.NEEDS_HUMAN, claimed=gone))
    assert st.state == ts.PAUSED


# -- a dialog is its own axis --------------------------------------------------

def test_a_pending_menu_does_not_overwrite_working_but_is_reported_beside_it():
    """A turn blocked on a question is RUNNING as far as the turn is concerned.
    Collapsing the two would draw a spinner over an unanswered dialog."""
    st = ts.derive(_turn(Turn.RUNNING, claimed=_Runner()), menu_pending=True)
    assert st.state == ts.WORKING
    assert st.menu_pending
    # A channel that only asks one question still gets the right answer.
    assert st.stuck


# -- terminal ------------------------------------------------------------------

@pytest.mark.parametrize(
    "turn_state,expected",
    [
        (Turn.DONE, ts.DONE),
        (Turn.CANCELLED, ts.CANCELLED),
        (Turn.MISSED, ts.MISSED),
        (Turn.FAILED, ts.FAILED),
        (Turn.LOST, ts.LOST),
    ],
)
def test_terminal_states(turn_state, expected):
    st = ts.derive(_turn(turn_state, claimed=_Runner()))
    assert st.state == expected
    assert st.settled and not st.pending
    assert not st.stuck


# -- the rescue offer ----------------------------------------------------------

def test_a_cloud_runner_rides_along_when_one_could_help():
    cloud = _Runner("cloud-1", rid="abc")
    st = ts.derive(_turn(Turn.QUEUED), reach=_reach("offline", [_Runner()]), cloud=cloud)
    assert st.cloud_runner == "cloud-1"
    # The id travels because acting on the offer needs one.
    assert st.cloud_runner_id == "abc"


def test_no_cloud_runner_is_not_an_error_it_is_just_no_offer():
    st = ts.derive(_turn(Turn.QUEUED), reach=_reach("offline", [_Runner()]))
    assert st.cloud_runner is None and st.cloud_runner_id is None


# -- the wire form -------------------------------------------------------------

def test_as_dict_is_flat_json_native_and_carries_the_derived_questions():
    seen = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc)
    gone = _Runner("jj-mbp", reachable=False, last_heartbeat_at=seen)
    d = ts.derive(_turn(Turn.RUNNING, claimed=gone), cloud=_Runner("cloud-1", rid="x")).as_dict()
    assert d == {
        "state": "paused",
        "agent_slug": None,
        "runners": ["jj-mbp"],
        "claimed_by": "jj-mbp",
        "pinned": False,
        "cloud_runner": "cloud-1",
        "cloud_runner_id": "x",
        "last_seen_at": "2026-09-20T12:00:00+00:00",
        "menu_pending": False,
        # Derived, not re-derived by four clients that could disagree.
        "settled": False,
        "stuck": True,
    }


def test_state_sets_partition_every_state():
    """A new state added to the module must be classified, or a client asking
    'is this still going' silently answers no for it."""
    all_states = {
        ts.PICKING_UP, ts.WAITING_RUNNER, ts.UNROUTED, ts.WORKING, ts.BLOCKED,
        ts.PAUSED, ts.DONE, ts.CANCELLED, ts.MISSED, ts.FAILED, ts.LOST,
    }
    assert ts.PENDING | ts.TERMINAL == all_states
    assert not (ts.PENDING & ts.TERMINAL)
    assert ts.STUCK <= ts.PENDING
