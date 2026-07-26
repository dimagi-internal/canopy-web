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


def test_another_tenants_agent_is_invisible(client):
    """`_visible_agent_workspace_ids` gates the WHOLE agents surface: tasks,
    board commands (a write), work products, skills, PUT /runners (a write),
    and GET /{slug}/turns/ (which serializes AgentTurnOut.share_token — a
    public transcript link). Non-membership must be indistinguishable from
    non-existence on every one of them.

    This used to build an UNHOMED agent, because `_visible_agent_workspace_ids`
    returned the caller's workspace ids **plus {None}** and a workspace-less
    agent was therefore visible to every authenticated user (security review
    2026-07-26, hole A). The `{None}` leg is gone and so is the row it admitted
    — an unhomed agent cannot be created at all (agents/0013; see
    tests/test_agent_workspace_not_null.py). Cross-tenant is what is left to
    prove."""
    stranger_owner = User.objects.create_user("stranger", "stranger@dimagi.com", "pw")
    # auto_join_domains=[] is load-bearing: the gate auto-joins the caller
    # first, so a domain-matching workspace would silently admit them.
    other = Workspace.objects.create(
        slug="other", display_name="Other", created_by=stranger_owner, auto_join_domains=[]
    )
    Agent.objects.create(slug="secret", name="Secret", workspace=other)

    list_body = client.get("/api/agents/").json()
    items = list_body["items"] if "items" in list_body else list_body
    assert not any(a["slug"] == "secret" for a in items)

    assert client.get("/api/agents/secret/").status_code == 404
