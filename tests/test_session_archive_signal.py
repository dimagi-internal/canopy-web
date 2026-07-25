"""The closing signal: a runner reporting an archived task retires its session row,
and re-opening the task in emdash brings it back."""
import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
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
    )
    return user, ws, runner


def test_an_archived_task_archives_its_session():
    _user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd"), _Reported("live")])
    services.replace_reported_sessions(runner, ws, [_Reported("live")], archived=["ddd"])

    by_key = {b.session_key: b.session for b in RunnerBinding.objects.select_related("session")}
    assert by_key["ddd"].status == Session.ARCHIVED
    assert by_key["live"].status == Session.ACTIVE


def test_reopening_a_task_unarchives_it():
    """The WRITTEN half must be cleared explicitly — unlike the derived staleness
    half, it does not heal itself."""
    _user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])
    services.replace_reported_sessions(runner, ws, [], archived=["ddd"])
    assert Session.objects.get().status == Session.ARCHIVED

    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])
    assert Session.objects.get().status == Session.ACTIVE


def test_a_runner_cannot_archive_another_runners_session():
    """session_key is an emdash task NAME and names collide across machines. Scope the
    archive to the reporting runner's own bindings or one laptop retires another's."""
    user, ws, runner_a = _ctx()
    runner_b = Runner.objects.create(
        name="jj-mini", workspace=ws, location=Runner.LOCAL, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), paired_by=user,
    )
    services.replace_reported_sessions(runner_a, ws, [_Reported("ddd")])
    binding_a = RunnerBinding.objects.get(runner=runner_a, session_key="ddd")
    services.replace_reported_sessions(runner_b, ws, [_Reported("ddd")])
    binding_b = RunnerBinding.objects.get(runner=runner_b, session_key="ddd")
    assert Session.objects.count() == 2
    assert binding_a.pk != binding_b.pk

    # Capture the two rows BEFORE the archive: the clear step nulls runner_id on
    # anything not re-reported, so keying the assertion on runner_id would test the
    # clear, not the cross-runner scoping this test is about.
    services.replace_reported_sessions(runner_b, ws, [], archived=["ddd"])
    binding_a.refresh_from_db()
    assert Session.objects.get(pk=binding_a.session_id).status == Session.ACTIVE  # A untouched
    assert Session.objects.get(pk=binding_b.session_id).status == Session.ARCHIVED
    assert binding_a.runner_id == runner_a.id  # A never reported, so A's live pointer stands


def test_an_unknown_archived_name_is_ignored():
    _user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd")])
    services.replace_reported_sessions(runner, ws, [_Reported("ddd")], archived=["never-existed", ""])
    assert Session.objects.get().status == Session.ACTIVE


def test_the_route_applies_the_archive_signal():
    """End-to-end through the runner-authed route, not just the service."""
    from django.test import Client as DjangoClient

    user, ws, runner = _ctx()
    c = DjangoClient()
    c.force_login(user)
    body = {
        "sessions": [{"emdash_task": "live", "project": "canopy-web"}],
        "archived": ["ddd"],
    }
    services.replace_reported_sessions(runner, ws, [_Reported("ddd"), _Reported("live")])
    resp = c.post(
        f"/api/harness/runners/{runner.id}/sessions",
        data=body,
        content_type="application/json",
    )
    assert resp.status_code == 200
    by_key = {b.session_key: b.session for b in RunnerBinding.objects.select_related("session")}
    assert by_key["ddd"].status == Session.ARCHIVED
    # The re-reported one must stay ACTIVE — catches `archived` landing in the
    # wrong argument slot (which would archive everything the runner reported).
    assert by_key["live"].status == Session.ACTIVE


def test_an_archived_session_drops_out_of_the_live_list():
    """The archive signal must also retire the LIVE pointer: list_visible_sessions
    filters on runner__isnull=False, so an archived binding that kept its runner FK
    would keep showing up in the supervisor's open-sessions list forever (the runner
    re-sends its whole recently-archived list on every report, so it never ages out)."""
    user, ws, runner = _ctx()
    services.replace_reported_sessions(runner, ws, [_Reported("ddd"), _Reported("live")])
    assert {r.emdash_task for r in services.list_visible_sessions(user)} == {"ddd", "live"}

    services.replace_reported_sessions(runner, ws, [_Reported("live")], archived=["ddd"])
    assert {r.emdash_task for r in services.list_visible_sessions(user)} == {"live"}
    assert RunnerBinding.objects.get(session_key="ddd").runner_id is None
