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
    # services.upsert_agent (the low-level service) does NOT home an agent — only
    # the API view (apps.agents.api.upsert_agent) does that, on the request's
    # pinned/default workspace. Home it explicitly here so these turns-endpoint
    # tests don't accidentally exercise the fail-open unhomed-agent path that
    # apps.agents.api._visible_agent_workspace_ids closed (security review
    # 2026-07-26, hole A) — an unhomed agent is invisible now, not ungated.
    from types import SimpleNamespace
    agent = services.upsert_agent(
        SimpleNamespace(slug="echo", name="Echo", description="", persona="", email="", avatar_url="")
    )
    agent.workspace = workspace
    agent.save(update_fields=["workspace"])
    return agent


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


def test_turns_are_invisible_for_an_unhomed_agent(authed_client):
    """Security review 2026-07-26, hole A: an unhomed agent used to be visible
    to ANY authenticated caller across the whole /api/agents surface — including
    this endpoint, whose AgentTurnOut serializes `share_token`, a public
    `/share/<token>` transcript link. `_get_agent_or_404` (via
    `_visible_agent_workspace_ids`) now fails CLOSED: an agent with no
    workspace is unresolvable, not universally readable."""
    from types import SimpleNamespace
    services.upsert_agent(
        SimpleNamespace(slug="orphan", name="Orphan", description="", persona="", email="", avatar_url="")
    )
    resp = authed_client.get("/api/agents/orphan/turns/?limit=10")
    assert resp.status_code == 404
