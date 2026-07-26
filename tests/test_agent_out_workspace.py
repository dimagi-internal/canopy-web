"""AgentOut must serialize the agent's workspace slug (fleet spans workspaces,
so clients need it to build the correct /w/<workspace>/agents/<slug> deep link
instead of assuming the active workspace — see commit 483c821)."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
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
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


def test_list_agents_serializes_workspace_slug(client, workspace):
    Agent.objects.create(slug="echo", name="Echo", workspace=workspace)

    body = client.get("/api/agents/").json()
    items = body["items"] if "items" in body else body
    echo = next(a for a in items if a["slug"] == "echo")
    assert echo["workspace"] == "canopy"


def test_agent_detail_serializes_workspace_slug(client, workspace):
    Agent.objects.create(slug="echo", name="Echo", workspace=workspace)

    body = client.get("/api/agents/echo/").json()
    assert body["workspace"] == "canopy"


def test_agent_with_no_workspace_is_invisible(client):
    """Superseded by the security review (2026-07-26, hole A): this test used
    to encode "an unhomed agent is visible on the flat compat-shim route" as
    intentional migration safety. It was the mirror image of the bug
    apps/harness/api.py::_agent_or_404 fixed as F1 — `_visible_agent_workspace_ids`
    returned the caller's workspace ids **plus {None}**, so any authenticated
    user (not just a workspace member) could see an unhomed agent across the
    WHOLE agents surface: tasks, board commands (a write), work products,
    skills, PUT /runners (a write), and GET /{slug}/turns/ (which serializes
    AgentTurnOut.share_token — a public transcript link). That is read+write,
    strictly broader than F1.

    Production carries zero agents with workspace_id IS NULL (verified at
    review time), and the real creation path (upsert_agent) always homes a
    new agent to a workspace — this is a legacy/pre-migration edge case, not
    a live user-facing flow. `_visible_agent_workspace_ids` now fails CLOSED:
    a workspace-less agent is unresolvable via this API until it's homed, not
    universally visible."""
    Agent.objects.create(slug="orphan", name="Orphan", workspace=None)

    list_body = client.get("/api/agents/").json()
    items = list_body["items"] if "items" in list_body else list_body
    assert not any(a["slug"] == "orphan" for a in items)

    assert client.get("/api/agents/orphan/").status_code == 404
