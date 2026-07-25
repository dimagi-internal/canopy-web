"""Manual archive: the escape hatch for a web chat (which no runner will ever close)
and for force-retiring a row without touching emdash."""
import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions.models import Session
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(user)
    return user, ws, c


def test_archive_then_unarchive_round_trips():
    user, ws, c = _ctx()
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web")

    resp = c.post(f"/api/canopy-sessions/{s.id}/archive")
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
    assert {r["id"] for r in c.get("/api/canopy-sessions/").json()} == set()

    resp = c.post(f"/api/canopy-sessions/{s.id}/unarchive")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    assert {r["id"] for r in c.get("/api/canopy-sessions/").json()} == {str(s.id)}


def test_archiving_twice_is_a_no_op_not_an_error():
    user, ws, c = _ctx()
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web")
    assert c.post(f"/api/canopy-sessions/{s.id}/archive").status_code == 200
    assert c.post(f"/api/canopy-sessions/{s.id}/archive").status_code == 200
    assert Session.objects.get(pk=s.pk).status == Session.ARCHIVED


def test_a_non_member_gets_404_not_403():
    """Same as every other route on this router — no existence leak across tenants."""
    _user, ws, _c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_WEB, title="web")
    other = User.objects.create_user("nope", "nope@dimagi.com", "pw")
    other_ws = Workspace.objects.create(slug="w2", display_name="W2", created_by=other)
    WorkspaceMembership.objects.create(user=other, workspace=other_ws, role=WorkspaceMembership.OWNER)
    c2 = Client()
    c2.force_login(other)
    assert c2.post(f"/api/canopy-sessions/{s.id}/archive").status_code == 404
    assert Session.objects.get(pk=s.pk).status == Session.ACTIVE
