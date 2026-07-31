"""A session says WHY its runner cannot act, not just that it cannot.

`runner_online` collapses every unreachable state into one bool, and the two that
matter most to a reader are opposite decisions: a PAUSED box is one a human parked
and can un-park from the Runners tab, while an offline one is a box to go look at.
The supervisor's Sessions tab holds both kinds back from the default list, so the
line explaining what it withheld is only honest if the payload distinguishes them.

Both fields read the SAME `Runner.live_status`, so they cannot disagree about a
runner — which is the point of deriving rather than storing a second flag.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import Session
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


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _rows(user):
    return _client(user).get("/api/canopy-sessions/").json()


def test_a_live_runner_reports_online():
    user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])

    (row,) = _rows(user)
    assert row["runner_online"] is True
    assert row["runner_status"] == "online"


def test_a_paused_runner_is_named_as_paused_not_merely_offline():
    """The distinction the Sessions tab renders: 'runner paused' (undo it here)
    vs 'runner offline' (go find out what happened)."""
    user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])

    runner.paused = True
    runner.save(update_fields=["paused"])

    (row,) = _rows(user)
    assert row["runner_status"] == "paused"
    # And the bool still agrees — a paused box takes no work, so the existing
    # placement banner keeps firing exactly as it did before this field existed.
    assert row["runner_online"] is False


def test_an_unbound_session_has_no_runner_status():
    """A web chat that has never sent has no runner yet — None, not 'offline'.
    Reporting it as unreachable would hide a chat the moment it was created."""
    user, ws, _runner = _ctx()
    Session.objects.create(
        workspace=ws, created_by=user, title="fresh", origin=Session.ORIGIN_WEB,
    )

    (row,) = _rows(user)
    assert row["runner_name"] is None
    assert row["runner_online"] is None
    assert row["runner_status"] is None


def test_runner_status_never_contradicts_runner_online():
    """Both derive from live_status, so no combination of stored state can make
    one say reachable while the other says parked."""
    user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])

    for paused, status in ((False, Runner.ONLINE), (True, Runner.ONLINE), (False, Runner.DEGRADED)):
        runner.paused, runner.status = paused, status
        runner.save(update_fields=["paused", "status"])

        (row,) = _rows(user)
        assert row["runner_online"] is (row["runner_status"] == "online")
