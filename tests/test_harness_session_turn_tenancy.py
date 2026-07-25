"""A chat-session turn must be tenant-gated like agent/project turns: a
non-member of the session's workspace gets 404 from the turn read routes,
never the transcript. Regression for the _turn_or_404 fall-through."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions.models import Session
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@dimagi.com", "pw")


@pytest.fixture()
def workspace(owner):
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def stranger():
    # email domain outside auto-join so membership stays empty
    return User.objects.create_user("stranger", "stranger@example.org", "pw")


@pytest.fixture()
def session_turn(workspace, owner):
    session = Session.objects.create(workspace=workspace, created_by=owner, title="t")
    return Turn.objects.create(chat_session=session, prompt="hi")


def _login(user):
    c = Client()
    c.force_login(user)
    return c


def test_member_reads_session_turn(owner, session_turn):
    c = _login(owner)
    assert c.get(f"/api/harness/turns/{session_turn.id}").status_code == 200
    assert c.get(f"/api/harness/turns/{session_turn.id}/events").status_code == 200


def test_stranger_gets_404_on_session_turn(stranger, session_turn):
    c = _login(stranger)
    assert c.get(f"/api/harness/turns/{session_turn.id}").status_code == 404
    assert c.get(f"/api/harness/turns/{session_turn.id}/events").status_code == 404
