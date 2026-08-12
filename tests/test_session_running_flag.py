""""Running" is what the engine says it is, not what its writes imply.

The list badge used to be inferred from activity recency: a session whose transcript
had grown in the last 120s was "running", anything else rendered as a plain "12m ago".
Silence is not idleness, so that reads wrong in both directions — a turn inside a long
tool call or a subagent writes nothing for minutes and the row says finished (reported
2026-08-12: "it looks like sessions finished that are still running when I click on
them"), while a turn that just stopped keeps a live green badge for two more minutes.

Emdash already knows: it stamps `agent_status` on a conversation when it starts and
stops driving the agent. These tests pin that a reported status WINS, that a runner
which cannot answer still gets the old heuristic, and that an offline runner's last
answer is not believed.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.harness import services
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

# Comfortably outside services.RUNNING_WINDOW (120s), so the recency heuristic alone
# would call every session below finished.
LONG_AGO = dt.timedelta(minutes=12)


class _Reported:
    """Duck-types ReportedSessionIn — services reads attributes, not dict keys."""

    def __init__(self, task, *, agent_status="", age=LONG_AGO, project="canopy-web"):
        self.emdash_task = task
        self.project = project
        self.status = "in_progress"
        self.agent_status = agent_status
        self.last_interacted_at = timezone.now() - age
        self.recent_messages = []


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="jj-air", workspace=ws, location=Runner.LOCAL, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), paired_by=user, host="jj@air",
        capabilities={"sessions": True},
    )
    return user, ws, runner


def _row(user):
    c = Client()
    c.force_login(user)
    (row,) = c.get("/api/canopy-sessions/").json()
    return row


def test_a_working_session_that_has_been_quiet_for_minutes_is_still_running():
    """The reported bug. A long tool call is silence, not completion."""
    user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("pov-grad", agent_status="working")])

    assert _row(user)["running"] is True


def test_a_session_the_engine_calls_idle_is_not_running_however_recently_it_wrote():
    """The same misreading in the other direction: a turn that stopped five seconds
    ago is finished, and the badge should say so now rather than in two minutes."""
    user, ws, runner = _ctx()
    services.replace_reported_sessions(
        runner, ws, [_Reported("hh", agent_status="awaiting-input", age=dt.timedelta(seconds=5))]
    )

    assert _row(user)["running"] is False


def test_a_runner_that_cannot_answer_still_gets_the_recency_heuristic():
    """Blank is "I don't know", not "idle" — an older runner, a cloud runner with no
    emdash, or a drifted schema. Those keep exactly the behaviour they had."""
    user, ws, runner = _ctx()

    services.replace_reported_sessions(runner, ws, [_Reported("kmc", age=dt.timedelta(seconds=5))])
    assert _row(user)["running"] is True

    services.replace_reported_sessions(runner, ws, [_Reported("kmc")])
    assert _row(user)["running"] is False


def test_an_offline_runners_last_answer_is_not_believed():
    """It describes a box that is no longer there — the session is parked, not live."""
    user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("labs", agent_status="working")])

    # Liveness is observed, not claimed: a lapsed heartbeat is how a box that died
    # mid-turn actually presents (it never gets to tell us it stopped).
    runner.last_heartbeat_at = timezone.now() - dt.timedelta(hours=1)
    runner.save(update_fields=["last_heartbeat_at"])

    assert _row(user)["running"] is False


def test_the_flag_clears_when_the_turn_ends():
    """Every report is a fresh observation, so "working" has to be retirable by the
    next one — a stuck badge is the failure this whole path is meant to remove."""
    user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd", agent_status="working")])
    assert _row(user)["running"] is True

    services.replace_reported_sessions(runner, ws, [_Reported("ddd", agent_status="awaiting-input")])
    assert _row(user)["running"] is False
