"""Nonsense ?limit=/?offset= must be a harmless no-op, never a 500.

`Page` declares `limit: int = Field(ge=1, le=500)` and `offset: int = Field(ge=0)`.
Any route that forwards a caller-supplied value straight through therefore fails
*inside* its own response model, where Django Ninja can only answer 500 — a
server error for what is plainly a client mistake.

These are end-to-end (real routes, real response models) on purpose: a unit test
of `clamp_limit` alone would still pass if a route forgot to call it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def workspace(user):
    # Security review 2026-07-26, hole A: an unhomed agent is now invisible
    # (apps.agents.api._visible_agent_workspace_ids fails closed), so the
    # `agent` fixture below must be homed like a real agent (upsert_agent
    # always homes one) — an unhomed fixture would 404 every per-agent route
    # exercised here, which is not what this pagination-clamping suite tests.
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture()
def agent(workspace):
    return Agent.objects.create(slug="echo", name="Echo", workspace=workspace)


# One case per distinct clamp shape across the paginated surface: the 500-cap
# routes, the 100-cap route (insights/projects), and the routes that also take a
# caller-supplied offset.
@pytest.mark.parametrize("path", [
    "/api/agents/",
    "/api/agents/echo/syncs/",
    "/api/agents/echo/turns/",
    "/api/agents/echo/work-products/",
    "/api/agents/echo/runs/",
    "/api/agents/echo/schedules/",
    "/api/projects/",
    "/api/insights/",
    "/api/issues/",
    "/api/shareouts/",
])
@pytest.mark.parametrize("query", ["limit=0", "limit=-5"])
def test_nonsense_limit_is_a_client_no_op_not_a_500(client, agent, path, query):
    resp = client.get(f"{path}?{query}")
    assert resp.status_code == 200, f"{path}?{query} → {resp.status_code}"
    assert resp.json()["limit"] == 1  # clamped to Page.limit's floor


@pytest.mark.parametrize("path", ["/api/projects/", "/api/issues/"])
def test_negative_offset_is_a_client_no_op_not_a_500(client, path):
    resp = client.get(f"{path}?offset=-1")
    assert resp.status_code == 200, f"{path} → {resp.status_code}"
    assert resp.json()["offset"] == 0


@pytest.mark.parametrize("path", ["/api/agents/", "/api/issues/"])
def test_oversized_limit_clamps_to_the_cap(client, agent, path):
    resp = client.get(f"{path}?limit=99999")
    assert resp.status_code == 200, f"{path} → {resp.status_code}"
    assert resp.json()["limit"] == 500


def test_insights_clamps_to_its_own_lower_cap(client):
    # not every route shares the 500 budget — the cap is per-route policy
    resp = client.get("/api/insights/?limit=99999")
    assert resp.status_code == 200
    assert resp.json()["limit"] == 100
