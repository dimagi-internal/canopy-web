"""API-level (ninja route) tests for the agent turns endpoints — reproduces the
live 500 the service-level tests missed."""
from __future__ import annotations

import pytest

from apps.agents import services
from apps.agents.models import Agent
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def authed_user(django_user_model):
    return django_user_model.objects.create_user(username="dev", email="dev@dimagi.com", password="pw")


@pytest.fixture()
def workspace(authed_user):
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=authed_user)
    WorkspaceMembership.objects.create(user=authed_user, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def authed_client(client, authed_user):
    client.force_login(authed_user)
    return client


def _echo(workspace: Workspace) -> Agent:
    # `workspace` is a required keyword on the service now: Agent.workspace is
    # NOT NULL (agents/0013), so the tenant is chosen before the row is written
    # rather than patched on afterwards.
    from types import SimpleNamespace
    return services.upsert_agent(
        SimpleNamespace(slug="echo", name="Echo", description="", persona="", email="", avatar_url=""),
        workspace=workspace,
    )


def test_list_turns_empty(authed_client, workspace):
    _echo(workspace)
    resp = authed_client.get("/api/agents/echo/turns/?limit=1")
    assert resp.status_code == 200, resp.content
    assert resp.json()["items"] == []


def test_post_then_list_turn(authed_client, workspace):
    _echo(workspace)
    resp = authed_client.post(
        "/api/agents/echo/turns/",
        data={"cli_session_id": "s1", "title": "Did a thing", "task_ext_ids": ["t1"],
              "work_product_urls": [], "source": "turn"},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    resp2 = authed_client.get("/api/agents/echo/turns/?limit=10")
    assert resp2.status_code == 200, resp2.content
    assert resp2.json()["items"][0]["task_ext_ids"] == ["t1"]


def test_turns_are_invisible_for_another_tenants_agent(authed_client):
    """Security review 2026-07-26, hole A: this endpoint's AgentTurnOut
    serializes `share_token`, a public `/share/<token>` transcript link, so
    `_get_agent_or_404` must fail CLOSED for an agent the caller cannot see.

    This used to construct an UNHOMED agent, because that was the strongest
    version of "cannot see" the model allowed. It no longer is — an unhomed
    agent cannot exist (agents/0013; see tests/test_agent_workspace_not_null.py)
    — so the case with something left to prove is the cross-tenant one."""
    from types import SimpleNamespace

    from apps.workspaces.testing import a_workspace

    services.upsert_agent(
        SimpleNamespace(slug="secret", name="Secret", description="", persona="", email="", avatar_url=""),
        # auto_join_domains=[] is load-bearing: _get_agent_or_404 auto-joins the
        # caller first, so a domain-matching workspace would silently admit them.
        workspace=a_workspace("other-tenant", auto_join_domains=[]),
    )
    resp = authed_client.get("/api/agents/secret/turns/?limit=10")
    assert resp.status_code == 404
