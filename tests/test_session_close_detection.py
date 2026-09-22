# ruff: noqa: F811 — the fixtures below are imported from test_slack and then requested by name.
"""Telling a CLOSED session from a runner that went quiet — and telling Slack.

emdash deletes a task it closes, leaving nothing to name, so the only sign of an
ordinary close is the task's absence from the runner's report. That is an
observation only when a report ARRIVED and held the runner's whole open set: a
laptop asleep sends no report, and a runner that cannot read emdash sends none
either. So: absent from two consecutive COMPLETE reports -> archived. And a
thread a session was shared into (bind) is told, once, and replies are refused.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner, Turn
from apps.harness.services import replace_reported_sessions
from apps.slack import share
from tests.test_slack import (  # noqa: F401
    alice, configured, installation, linked, mention, slack, ws,
)

pytestmark = pytest.mark.django_db
POSTED_TS = "1700000999.000100"


def _reported(task, project="canopy-web"):
    return SimpleNamespace(emdash_task=task, project=project, status="running",
                           last_interacted_at=None, recent_messages=[])


@pytest.fixture
def runner(ws):
    return Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL, host="mbp")


def _session(runner, ws, task="slack"):
    replace_reported_sessions(runner, ws, [_reported(task)], complete=True)
    return Session.objects.get(runner_binding__session_key=task)


def test_absence_from_two_complete_reports_closes_it(runner, ws):
    session = _session(runner, ws)
    replace_reported_sessions(runner, ws, [], complete=True)
    session.refresh_from_db()
    assert session.status == Session.ACTIVE          # one miss is a flicker
    replace_reported_sessions(runner, ws, [], complete=True)
    session.refresh_from_db()
    assert session.status == Session.ARCHIVED


def test_an_incomplete_report_proves_nothing(runner, ws):
    """Cut off by the runner's limit — an old open task can simply be past the end."""
    session = _session(runner, ws)
    for _ in range(3):
        replace_reported_sessions(runner, ws, [], complete=False)
    session.refresh_from_db()
    assert session.status == Session.ACTIVE


def test_a_runner_that_goes_quiet_closes_nothing(runner, ws):
    """A laptop asleep sends no report at all — so nothing here is even called."""
    session = _session(runner, ws)
    session.refresh_from_db()
    assert session.status == Session.ACTIVE and session.runner_binding.missed_reports == 0


def test_being_reported_again_resets_the_count(runner, ws):
    session = _session(runner, ws)
    replace_reported_sessions(runner, ws, [], complete=True)
    replace_reported_sessions(runner, ws, [_reported("slack")], complete=True)
    replace_reported_sessions(runner, ws, [], complete=True)
    session.refresh_from_db()
    assert session.status == Session.ACTIVE


def test_another_runners_report_does_not_count_against_it(runner, ws):
    session = _session(runner, ws)
    other = Runner.objects.create(name="other", workspace=ws, location=Runner.LOCAL, host="box2")
    for _ in range(3):
        replace_reported_sessions(other, ws, [], complete=True)
    session.refresh_from_db()
    assert session.status == Session.ACTIVE


# ---- and Slack hears about it --------------------------------------------------

@pytest.fixture
def shared(slack, linked, alice, runner, ws, django_capture_on_commit_callbacks):
    session = _session(runner, ws)
    assert share.share_session(alice, channel="C1", summary="working on it", mode=share.BIND,
                               session=session).ok
    return session


def _close(runner, ws, capture):
    with capture(execute=True):
        replace_reported_sessions(runner, ws, [], complete=True)
    with capture(execute=True):
        replace_reported_sessions(runner, ws, [], complete=True)


def test_a_shared_thread_is_told_once_when_its_session_closes(
        slack, shared, runner, ws, django_capture_on_commit_callbacks):
    _close(runner, ws, django_capture_on_commit_callbacks)
    notices = [p for p in slack.said("chat.postMessage") if "was closed" in p["text"]]
    assert len(notices) == 1 and notices[0]["thread_ts"] == POSTED_TS
    # Another report of the same closed session says nothing more.
    with django_capture_on_commit_callbacks(execute=True):
        replace_reported_sessions(runner, ws, [], complete=True)
    assert len([p for p in slack.said("chat.postMessage") if "was closed" in p["text"]]) == 1


def test_an_archive_in_emdash_tells_the_thread_too(
        slack, shared, runner, ws, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        replace_reported_sessions(runner, ws, [], archived=["slack"])
    assert any("was closed" in p["text"] for p in slack.said("chat.postMessage"))


def test_a_reply_to_a_closed_shared_session_is_refused(
        slack, shared, runner, ws, django_capture_on_commit_callbacks):
    _close(runner, ws, django_capture_on_commit_callbacks)
    mention("are you still there?", ts="1700001000.000100", thread_ts=POSTED_TS)
    assert not Turn.objects.exists()
    assert "was closed" in slack.said("chat.postEphemeral")[-1]["text"]


def test_a_quiet_laptop_never_posts_a_closed_notice(slack, shared, runner, ws):
    assert not any("was closed" in p["text"] for p in slack.said("chat.postMessage"))
    assert RunnerBinding.objects.get(session=shared).missed_reports == 0
