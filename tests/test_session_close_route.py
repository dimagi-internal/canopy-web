"""The HTTP surface. Refusals are 200 with ok:false — a session can go stale
between the phone rendering the list and a thumb reaching it."""
import pytest
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(user)
    return user, ws, c


def test_closing_a_web_session_archives_it_and_drops_it_from_the_list():
    user, ws, c = _ctx()
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web"
    )
    resp = c.post(f"/api/canopy-sessions/{s.id}/close")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "closing": False, "reason": ""}
    assert c.get("/api/canopy-sessions/").json() == []


def test_closing_a_reported_session_reports_closing_and_leaves_it_listed():
    """It is still open until the runner says otherwise. Saying so is the honest
    answer; the client renders a pending state."""
    user, ws, c = _ctx()
    runner = Runner.objects.create(
        name="jj-mbp", kind="laptop", host="jj-mbp", paired_by=user, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_RUNNER, title="ddd"
    )
    RunnerBinding.objects.create(
        session=s, runner=runner, host=runner.host, session_key="ddd",
        thread_key="emdash:ddd", live_seen_at=timezone.now(), reported_at=timezone.now(),
    )
    with patch("apps.realtime.groups.publish"):
        resp = c.post(f"/api/canopy-sessions/{s.id}/close")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "closing": True, "reason": ""}
    assert [r["id"] for r in c.get("/api/canopy-sessions/").json()] == [str(s.id)]


def test_a_refusal_is_200_with_a_reason():
    user, ws, c = _ctx()
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB,
        title="done", status=Session.ARCHIVED,
    )
    resp = c.post(f"/api/canopy-sessions/{s.id}/close")
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "closing": False, "reason": "already_closed"}


def test_a_non_member_gets_404_not_403():
    user, ws, _c = _ctx()
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web"
    )
    other = User.objects.create_user("nope", "nope@dimagi.com", "pw")
    c2 = Client()
    c2.force_login(other)
    assert c2.post(f"/api/canopy-sessions/{s.id}/close").status_code == 404
