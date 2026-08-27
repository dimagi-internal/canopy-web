"""Deleting a workspace, and deleting an agent — the two one-way doors.

Both `POST /api/workspaces/` and `POST /api/agents/` were create-only, so a
typo'd slug was permanent and fleet-visible. That made the onboarding path
un-rehearsable: you could not walk a new operator's steps end to end without
leaving a fake tenant and a fake agent behind forever.

RBAC differs between the two on purpose, and these tests pin that:
workspace delete is owner-only (it removes a tenant); agent delete needs
editor or owner — one step above the "any member" bar for creating one.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(email):
    return User.objects.create(username=email, email=email)


def _client(u):
    c = Client()
    c.force_login(u)
    return c


def _post(c, url, data=None):
    return c.post(url, data=json.dumps(data or {}), content_type="application/json")


def _ws(owner, slug="acme"):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    return ws


def _agent(ws, owner, slug="scout"):
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=ws, owner=owner)


# ---------------------------------------------------------------- workspaces

def test_owner_can_delete_an_empty_workspace():
    owner = _user("a@dimagi.com")
    _ws(owner)
    r = _client(owner).delete("/api/workspaces/acme/")
    assert r.status_code == 204
    assert not Workspace.objects.filter(slug="acme").exists()


def test_editor_cannot_delete_a_workspace():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    editor = _user("b@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=editor, role=WorkspaceMembership.EDITOR)

    r = _client(editor).delete("/api/workspaces/acme/")
    assert r.status_code == 403
    assert Workspace.objects.filter(slug="acme").exists()


def test_non_member_gets_404_not_403():
    """No existence leak — a stranger must not learn the workspace is real."""
    owner = _user("a@dimagi.com")
    _ws(owner)
    r = _client(_user("stranger@dimagi.com")).delete("/api/workspaces/acme/")
    assert r.status_code == 404
    assert Workspace.objects.filter(slug="acme").exists()


def test_workspace_with_agents_is_refused_with_409_naming_them():
    """`Agent.workspace` is PROTECT, so an unchecked delete would be a 500.
    The guard turns it into an actionable 409 that names what is in the way."""
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    _agent(ws, owner, "scout")
    _agent(ws, owner, "recon")

    r = _client(owner).delete("/api/workspaces/acme/")

    assert r.status_code == 409
    body = r.json()["detail"] if "detail" in r.json() else json.dumps(r.json())
    assert "recon" in body and "scout" in body
    assert Workspace.objects.filter(slug="acme").exists()


def test_workspace_delete_succeeds_once_its_agents_are_gone():
    """The documented recovery path in the 409 actually works."""
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    _agent(ws, owner, "scout")
    c = _client(owner)

    assert c.delete("/api/workspaces/acme/").status_code == 409
    assert c.delete("/api/agents/scout/").status_code == 204
    assert c.delete("/api/workspaces/acme/").status_code == 204

    assert not Workspace.objects.filter(slug="acme").exists()


# -------------------------------------------------------------------- agents

def test_editor_can_delete_an_agent():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    editor = _user("b@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=editor, role=WorkspaceMembership.EDITOR)
    _agent(ws, owner)

    r = _client(editor).delete("/api/agents/scout/")
    assert r.status_code == 204
    assert not Agent.objects.filter(slug="scout").exists()


def test_viewer_cannot_delete_an_agent():
    """Deleting is gated one step above creating (any member may upsert)."""
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    viewer = _user("b@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    _agent(ws, owner)

    r = _client(viewer).delete("/api/agents/scout/")
    assert r.status_code == 403
    assert Agent.objects.filter(slug="scout").exists()


def test_deleting_an_agent_in_another_tenant_is_404():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    _agent(ws, owner)

    outsider = _user("b@dimagi.com")
    _ws(outsider, slug="other")

    r = _client(outsider).delete("/api/agents/scout/")
    assert r.status_code == 404
    assert Agent.objects.filter(slug="scout").exists()


def test_deleting_an_agent_takes_its_children_with_it():
    """Every FK into Agent is CASCADE/SET_NULL — a real delete, nothing dangling."""
    from apps.agents.models import AgentTask

    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    agent = _agent(ws, owner)
    AgentTask.objects.create(agent=agent, ext_id="T1", title="something")

    assert _client(owner).delete("/api/agents/scout/").status_code == 204
    assert not AgentTask.objects.filter(agent_id=agent.id).exists()
