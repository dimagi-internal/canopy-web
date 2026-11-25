"""A runner's session report that is identical to its last one skips the reconcile.

Every box posts its whole open set every ~10s, and almost every report says exactly
what the one before it said. Such a report only restamps liveness on the same
bindings (`session_sync._replay_unchanged`): no row locks, no per-session rebuild,
no push of what viewers already have. A changed report, an expired memo, or a
binding that is no longer this runner's runs the full path.
"""
from __future__ import annotations

import time
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness import session_sync
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    cache.clear()
    me = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=me)
    WorkspaceMembership.objects.create(user=me, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", owner=me, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    client = Client()
    client.force_login(me)
    return {"me": me, "ws": ws, "runner": runner, "client": client}


def _body(n=10, **extra):
    return {"sessions": [{"emdash_task": f"task-{i}", "project": "canopy-web",
                          "agent_status": "awaiting-input",
                          "recent_messages": [{"role": "assistant", "text": f"done {i}"}]}
                         for i in range(n)], "complete": True, **extra}


def _post(world, body):
    with CaptureQueriesContext(connection) as q:
        r = world["client"].post(f"/api/harness/runners/{world['runner'].id}/sessions",
                                 body, content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json(), q.captured_queries


def test_an_unchanged_report_only_restamps_liveness(world):
    _post(world, _body())                      # creates
    _, full = _post(world, _body(n=10, archived=["x"]))  # a different report: the full path
    before = RunnerBinding.objects.get(session_key="task-3").live_seen_at
    out, replay = _post(world, _body(n=10, archived=["x"]))
    assert out == {"count": 10, "skipped": 0}
    # No locked load, no per-session save, no revive scan.
    assert len(replay) < len(full) / 2, (len(replay), len(full))
    assert not any("FOR UPDATE" in q["sql"] for q in replay)
    assert RunnerBinding.objects.get(session_key="task-3").live_seen_at > before


def test_a_changed_report_runs_the_full_path(world):
    _post(world, _body())
    _post(world, _body())
    body = _body()
    body["sessions"][2]["recent_messages"] = [{"role": "assistant", "text": "something new"}]
    _post(world, body)
    assert RunnerBinding.objects.get(session_key="task-2").tail == [
        {"role": "assistant", "text": "something new"}]


def test_the_memo_expires_and_the_full_reconcile_runs_again(world):
    _post(world, _body(n=2))
    _post(world, _body(n=2))
    # Someone archived a session while its task stays open: only the full path revives it.
    Session.objects.filter(runner_binding__session_key="task-1").update(status=Session.ARCHIVED)
    _post(world, _body(n=2))
    assert Session.objects.get(runner_binding__session_key="task-1").status == Session.ARCHIVED
    later = time.time() + session_sync.REPORT_RECONCILE_S + 1
    with mock.patch("apps.harness.session_sync.time.time", return_value=later):
        _post(world, _body(n=2))
    assert Session.objects.get(runner_binding__session_key="task-1").status == Session.ACTIVE


def test_a_binding_moved_off_this_runner_forces_the_full_path(world):
    _post(world, _body(n=2))
    other = Runner.objects.create(name="other", kind=Runner.EMDASH, host="other",
                                  owner=world["me"], workspace=world["ws"])
    RunnerBinding.objects.filter(session_key="task-0").update(runner=other)
    _post(world, _body(n=2))
    # The full path ran: this runner holds a binding for the task again.
    assert RunnerBinding.objects.filter(session_key="task-0", runner=world["runner"]).exists()


def test_two_identical_complete_reports_still_close_a_missing_session(world):
    """Absence from two complete reports closes a session; the second report is
    identical to the first and must still count."""
    _post(world, _body(n=2))
    _post(world, _body(n=1))
    _post(world, _body(n=1))
    assert Session.objects.get(runner_binding__session_key="task-1").status == Session.ARCHIVED


def test_a_session_whose_running_state_flips_on_the_clock_is_not_replayed_past_it(world):
    """A blank `agent_status` is read from recency: the session stops counting as
    running RUNNING_WINDOW after its last interaction, with no new report."""
    body = _body(n=1)
    body["sessions"][0]["agent_status"] = ""
    body["sessions"][0]["last_interacted_at"] = timezone.now().isoformat()
    _post(world, body)
    memo = cache.get(session_sync._report_memo_key(world["runner"]))
    assert memo["until"] <= time.time() + 121


def test_an_unchanged_report_pushes_nothing_to_viewers(world):
    _post(world, _body(n=2))
    with mock.patch("apps.realtime.signals.groups.publish") as publish, \
            mock.patch("apps.harness.session_sync.transaction.on_commit", side_effect=lambda f: f()):
        _post(world, _body(n=2))
    publish.assert_not_called()
