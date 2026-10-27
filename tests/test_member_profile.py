"""A member's page in a workspace: every member sees the basics, and only
someone who manages members sees the admin detail."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db

M = WorkspaceMembership
WS = "profile-ws"


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def team():
    ws = a_workspace(WS)
    other = a_workspace("profile-other")
    owner = a_member(ws, email="p-owner@dimagi.com", role=M.OWNER)
    viewer = a_member(ws, email="p-viewer@dimagi.com", role=M.VIEWER)
    editor = a_member(ws, email="p-editor@dimagi.com", role=M.EDITOR)
    M.objects.create(workspace=other, user=editor, role=M.ADMIN)
    M.objects.create(workspace=other, user=viewer, role=M.VIEWER)
    Agent.objects.create(slug="pbot", name="P Bot", workspace=ws, owner=editor)
    bot_user = get_user_model().objects.create_user(username="pbot@dimagi-ai.com", email="pbot@dimagi-ai.com")
    M.objects.create(workspace=ws, user=bot_user, role=M.EDITOR)
    Agent.objects.filter(slug="pbot").update(user=bot_user)
    return {"ws": ws, "owner": owner, "viewer": viewer, "editor": editor, "bot": bot_user}


def _get(user, target):
    return _client(user).get(f"/api/workspaces/{WS}/members/{target.id}/profile/")


def test_viewer_sees_basics_not_admin_detail(team):
    r = _get(team["viewer"], team["editor"])
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["email"] == "p-editor@dimagi.com"
    assert body["role"] == M.EDITOR
    assert body["kind"] == "person"
    assert body["is_you"] is False
    assert [a["slug"] for a in body["operates"]] == ["pbot"]
    # Both are in profile-other, so it is shown, with the member's role there.
    assert body["shared_workspaces"] == [
        {"slug": "profile-other", "display_name": body["shared_workspaces"][0]["display_name"], "role": M.ADMIN}
    ]
    assert body["admin"] is None


def test_owner_sees_admin_detail(team):
    body = _get(team["owner"], team["editor"]).json()
    assert body["admin"] is not None
    assert body["admin"]["chats_started"] == 0
    # The owner is not in profile-other, so it is not revealed to them.
    assert body["shared_workspaces"] == []


def test_agent_login_is_an_agent(team):
    body = _get(team["viewer"], team["bot"]).json()
    assert body["kind"] == "agent"
    assert body["agent"] == {"slug": "pbot", "name": "P Bot"}


def test_self_is_you(team):
    assert _get(team["viewer"], team["viewer"]).json()["is_you"] is True


def test_non_member_target_and_caller_404(team):
    stranger = a_member(a_workspace("profile-elsewhere"), email="p-stranger@dimagi.com", role=M.OWNER)
    assert _get(team["viewer"], stranger).status_code == 404
    assert _get(stranger, team["viewer"]).status_code == 404
