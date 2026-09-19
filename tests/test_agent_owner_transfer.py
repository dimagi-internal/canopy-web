"""Transferring an agent's ownership — a person in the canopy UI, and nobody else.

The owner is whose GitHub grant the agent's GitHub-backed features read through
(skill history today), so changing it is a decision about a credential. It is
deliberately NOT reachable by a machine: a PAT, the embedded widget's delegated
token, or a contact token — anything arriving as `Authorization: Bearer` — is
refused, so no agent, plugin or assistant can move ownership.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.agents.models import Agent
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def world():
    ws_owner = User.objects.create_user(username="o", email="o@dimagi.com", first_name="Olu")
    editor = User.objects.create_user(username="e", email="e@dimagi.com")
    viewer = User.objects.create_user(username="v", email="v@dimagi.com")
    outsider = User.objects.create_user(username="x", email="x@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=ws_owner)
    other = Workspace.objects.create(slug="elsewhere", display_name="Elsewhere", created_by=outsider)
    WorkspaceMembership.objects.create(workspace=ws, user=ws_owner, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=editor, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    WorkspaceMembership.objects.create(workspace=other, user=outsider, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws)
    return {"ws_owner": ws_owner, "editor": editor, "viewer": viewer, "outsider": outsider, "agent": agent}


def _put(client, user_id):
    return client.put("/api/agents/ace/owner", data={"user_id": user_id}, content_type="application/json")


def test_a_workspace_owner_assigns_an_owner_and_detail_reports_it(client, world):
    client.force_login(world["ws_owner"])
    r = _put(client, world["editor"].pk)
    assert r.status_code == 200, r.content
    world["agent"].refresh_from_db()
    assert world["agent"].owner_id == world["editor"].pk

    detail = client.get("/api/agents/ace/").json()
    assert detail["owner"] == {"user_id": world["editor"].pk, "name": "e@dimagi.com", "email": "e@dimagi.com"}
    assert detail["can_transfer_owner"] is True


def test_the_current_agent_owner_can_hand_it_on(client, world):
    world["agent"].owner = world["editor"]
    world["agent"].save()
    client.force_login(world["editor"])
    assert _put(client, world["viewer"].pk).status_code == 200
    world["agent"].refresh_from_db()
    assert world["agent"].owner_id == world["viewer"].pk


def test_an_editor_who_is_not_the_owner_cannot(client, world):
    client.force_login(world["editor"])
    assert _put(client, world["editor"].pk).status_code == 403
    assert client.get("/api/agents/ace/").json()["can_transfer_owner"] is False


def test_a_non_member_gets_404_not_403(client, world):
    client.force_login(world["outsider"])
    assert _put(client, world["outsider"].pk).status_code == 404


def test_the_new_owner_must_be_a_member_of_the_agents_workspace(client, world):
    client.force_login(world["ws_owner"])
    r = _put(client, world["outsider"].pk)
    assert r.status_code == 422
    world["agent"].refresh_from_db()
    assert world["agent"].owner_id is None


def test_only_a_workspace_owner_can_clear_ownership(client, world):
    world["agent"].owner = world["editor"]
    world["agent"].save()
    client.force_login(world["editor"])
    assert _put(client, None).status_code == 403
    client.force_login(world["ws_owner"])
    assert _put(client, None).status_code == 200
    world["agent"].refresh_from_db()
    assert world["agent"].owner_id is None


def test_a_bearer_token_is_refused_even_for_a_workspace_owner(world):
    """The whole point: no machine caller can move ownership."""
    from django.test import Client

    raw, _ = PersonalToken.create_for_user(user=world["ws_owner"], label="test")
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    r = c.put("/api/agents/ace/owner", data={"user_id": world["editor"].pk}, content_type="application/json")
    assert r.status_code == 403
    world["agent"].refresh_from_db()
    assert world["agent"].owner_id is None


def test_a_bearer_header_alongside_a_session_is_still_refused(client, world):
    """The embedded widget is same-origin, so its requests carry the session
    cookie AND a delegated bearer token. A header of any kind means a machine is
    in the loop."""
    client.force_login(world["ws_owner"])
    r = client.put("/api/agents/ace/owner", data={"user_id": world["editor"].pk},
                   content_type="application/json", HTTP_AUTHORIZATION="Bearer anything")
    assert r.status_code == 403
