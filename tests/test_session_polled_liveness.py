"""Session liveness is POLLED, not evented.

The runner reports its whole open-task set at least every `session_report_seconds`
(10s), so "was this binding in a recent report" is a reliable, self-healing liveness
signal. Everything else was not: emdash DELETES a closed task rather than setting
`archived_at`, so `list_recently_archived_tasks` returns [] forever and the written
closing signal never fires. Depending on it left 47 zombie sessions `active` with
their runner FK stripped (observed on labs 2026-07-25).

Two invariants together:
  * `RunnerBinding.runner` is durable IDENTITY — which box this session lives on. A
    report never nulls it, so every session always knows its runner.
  * `RunnerBinding.live_seen_at` is the liveness CLOCK — stamped on every reported
    session, read against SESSION_LIVE_WINDOW.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.canopy_sessions.staleness import SESSION_LIVE_WINDOW
from apps.harness import services
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


class _Reported:
    """Duck-types ReportedSessionIn — services reads attributes, not dict keys."""

    def __init__(self, task, project="canopy-web"):
        self.emdash_task = task
        self.project = project
        self.status = "in_progress"
        self.last_interacted_at = timezone.now()
        self.recent_messages = []


def _ctx(host="jj@air"):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="jj-air", workspace=ws, location=Runner.LOCAL, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), paired_by=user, host=host,
        capabilities={"sessions": True},
    )
    return user, ws, runner


def _age_binding(session_key, delta):
    """Backdate a binding's liveness clock — i.e. it fell off the report `delta` ago."""
    RunnerBinding.objects.filter(session_key=session_key).update(
        live_seen_at=timezone.now() - delta
    )


# --- identity: the runner FK survives falling off the report -----------------


def test_a_session_that_fell_off_the_report_keeps_its_runner():
    """emdash deleted the task, so it is simply absent from the next report. The
    session must still know which box it belonged to — nulling the FK is what
    produced 47 runner-less rows on labs."""
    _user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd"), _Reported("live")])

    services.replace_reported_sessions(runner, ws, [_Reported("live")])

    gone = RunnerBinding.objects.get(session_key="ddd")
    assert gone.runner_id == runner.id, "identity must survive absence from a report"


def test_an_explicit_archive_also_keeps_the_runner():
    """The written closing signal retires the SESSION; it does not erase which box
    the session lived on."""
    _user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd"), _Reported("live")])

    services.replace_reported_sessions(runner, ws, [_Reported("live")], archived=["ddd"])

    gone = RunnerBinding.objects.get(session_key="ddd")
    assert gone.session.status == Session.ARCHIVED
    assert gone.runner_id == runner.id


# --- liveness: the polled window decides `state=active` ----------------------


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def test_active_drops_a_session_once_it_stops_being_reported():
    """No `archived` signal involved — emdash never sends one. Falling out of the
    report for longer than the window IS the retirement."""
    user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd"), _Reported("live")])
    services.replace_reported_sessions(runner, ws, [_Reported("live")])
    _age_binding("ddd", SESSION_LIVE_WINDOW + dt.timedelta(minutes=1))

    ids = {r["title"] for r in _client(user).get("/api/canopy-sessions/").json()}
    assert ids == {"live"}


def test_a_session_just_inside_the_window_survives():
    """A brief report gap (a runner restart, a swallowed POST) must not retire
    anything — the window is many multiples of the 10s report cadence."""
    user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])
    _age_binding("ddd", SESSION_LIVE_WINDOW - dt.timedelta(seconds=30))

    ids = {r["title"] for r in _client(user).get("/api/canopy-sessions/").json()}
    assert ids == {"ddd"}


def test_a_re_reported_session_comes_back_by_itself():
    """Derived, never written: the runner comes back and the row un-retires with no
    repair step. This is the property that makes polling safe."""
    user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])
    _age_binding("ddd", SESSION_LIVE_WINDOW + dt.timedelta(minutes=1))
    assert _client(user).get("/api/canopy-sessions/").json() == []

    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])

    ids = {r["title"] for r in _client(user).get("/api/canopy-sessions/").json()}
    assert ids == {"ddd"}


def test_every_active_session_reports_its_runner():
    """The user-visible contract: no row in the list is missing its runner."""
    user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("a"), _Reported("b")])

    rows = _client(user).get("/api/canopy-sessions/").json()
    assert rows and all(r["runner_name"] == "jj-air" for r in rows)
    assert all(r["runner_online"] is True for r in rows)


def test_an_offline_runners_sessions_leave_the_active_list():
    """The whole point: a box that is not reporting cannot take a message, so its
    sessions must not be offered. Nothing is deleted — they return when it does."""
    user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])
    # The box goes away: it stops reporting AND stops heartbeating.
    _age_binding("ddd", SESSION_LIVE_WINDOW + dt.timedelta(minutes=1))
    Runner.objects.filter(pk=runner.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1)
    )

    c = _client(user)
    assert c.get("/api/canopy-sessions/").json() == []
    archived = {r["title"] for r in c.get("/api/canopy-sessions/?state=archived").json()}
    assert archived == {"ddd"}, "retired, not destroyed"


# --- the live-sessions projection (the supervisor WS push) -------------------


def test_list_visible_sessions_drops_an_archived_session_immediately():
    """An explicit archive must not wait out the window — it is a decision, not a
    staleness observation. (This is what the nulled runner FK used to enforce.)"""
    user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd"), _Reported("live")])
    assert {r.emdash_task for r in services.list_visible_sessions(user)} == {"ddd", "live"}

    services.replace_reported_sessions(runner, ws, [_Reported("live")], archived=["ddd"])

    assert {r.emdash_task for r in services.list_visible_sessions(user)} == {"live"}


def test_list_visible_sessions_drops_a_session_past_the_window():
    user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd"), _Reported("live")])
    _age_binding("ddd", SESSION_LIVE_WINDOW + dt.timedelta(minutes=1))

    assert {r.emdash_task for r in services.list_visible_sessions(user)} == {"live"}


# --- claim routing: a stale binding must not wedge the session ---------------


def _session_turn(ws, session, user):
    from apps.harness.models import Turn

    return Turn.objects.create(
        workspace=ws, chat_session=session, status=Turn.QUEUED,
        prompt="hi", enqueued_by=user,
    )


def test_stickiness_survives_the_liveness_change():
    """Liveness governs what is LISTED, never where a turn runs. Continuing a chat on
    another box means a fresh emdash session with none of the conversation's context,
    so that stays the user's explicit choice (the placement banner) rather than
    something a staleness timer does silently — spec 2026-07-24.

    Guards against 'fixing' the stuck-send by quietly relaxing stickiness: the real
    defect was the banner never appearing, not the routing.
    """
    user, ws, holder = _ctx()
    services.replace_reported_sessions(holder, ws, [_Reported("ddd")])
    _age_binding("ddd", SESSION_LIVE_WINDOW + dt.timedelta(minutes=1))
    session = RunnerBinding.objects.get(session_key="ddd").session
    _session_turn(ws, session, user)

    other = Runner.objects.create(
        name="cloud", workspace=ws, location=Runner.CLOUD, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), paired_by=user, ready=True,
        capabilities={"sessions": True},
    )
    assert services.claim_next_turn(other) is None, "no silent failover, even when stale"
    assert services.claim_next_turn(holder) is not None, "the holder still owns it"
