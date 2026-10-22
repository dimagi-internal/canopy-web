"""PUT /api/canopy-sessions/{id}/title — renaming a session by hand (the /ide
rename). A typed title must survive the runner's report loop, which only ever
replaces titles canopy generated."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions import services as chat
from apps.harness.session_sync import _title_is_derived
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def workspace(user):
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def session(workspace, user):
    return chat.create_session(workspace=workspace, created_by=user, project="canopy-web", title="")


def _put(c, session, title):
    return c.put(f"/api/canopy-sessions/{session.id}/title", {"title": title},
                 content_type="application/json")


def test_rename_sets_a_title_the_report_loop_keeps(session, user):
    c = Client()
    c.force_login(user)
    r = _put(c, session, "  Login   bug\n")
    assert r.status_code == 200
    assert r.json()["title"] == "Login bug"
    session.refresh_from_db()
    assert session.title == "Login bug"
    assert _title_is_derived(session, "emdash:canopy-web-1234") is False


def test_blank_clears_the_title(session, user):
    c = Client()
    c.force_login(user)
    _put(c, session, "Something")
    assert _put(c, session, "   ").status_code == 200
    session.refresh_from_db()
    assert session.title == ""


def test_a_non_member_cannot_rename(session):
    stranger = User.objects.create_user("x", "x@example.com", "pw")
    c = Client()
    c.force_login(stranger)
    assert _put(c, session, "Mine now").status_code == 404
    session.refresh_from_db()
    assert session.title == ""
