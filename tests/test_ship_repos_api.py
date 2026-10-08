"""PATCH /api/agents/<slug>/ship-repos — the owner's standing ship grant.

Owner or admin only: like `auto`, it widens what the agent does without asking. And
never through the agent-repo upsert, so an agent cannot grant itself (`AgentIn` has
no such field — asserted below).
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.agents.schemas import AgentIn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture()
def eva():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    editor = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=editor, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=owner)
    return owner, editor, agent


def _c(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def test_the_owner_sets_and_clears_ship_repos(eva):
    owner, _editor, agent = eva
    r = _c(owner).patch("/api/agents/eva/ship-repos",
                        {"ship_repos": ["dimagi-internal/eva",
                                        "https://github.com/dimagi-internal/chrome-sales.git",
                                        "dimagi-internal/eva"]},
                        content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json() == {"ship_repos": ["dimagi-internal/eva", "dimagi-internal/chrome-sales"]}
    agent.refresh_from_db()
    assert agent.ship_repos == ["dimagi-internal/eva", "dimagi-internal/chrome-sales"]
    assert _c(owner).get("/api/agents/eva/").json()["ship_repos"] == agent.ship_repos
    r = _c(owner).patch("/api/agents/eva/ship-repos", {"ship_repos": []},
                        content_type="application/json")
    assert r.json() == {"ship_repos": []}


def test_an_editor_cannot_widen_the_grant(eva):
    _owner, editor, _agent = eva
    r = _c(editor).patch("/api/agents/eva/ship-repos", {"ship_repos": ["dimagi-internal/eva"]},
                         content_type="application/json")
    assert r.status_code == 403


def test_a_malformed_repo_is_refused(eva):
    owner, _editor, agent = eva
    r = _c(owner).patch("/api/agents/eva/ship-repos", {"ship_repos": ["not a repo"]},
                        content_type="application/json")
    assert r.status_code == 422
    agent.refresh_from_db()
    assert agent.ship_repos == []


def test_the_repo_upsert_cannot_carry_it():
    assert "ship_repos" not in AgentIn.model_fields
