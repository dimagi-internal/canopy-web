"""GET /api/agents/{slug}/runtime — the Agent Runtime Registry discovery endpoint.

A runner asks canopy-web "how do I run agent X?" and gets the repo pointer, the
secret-reference names to resolve, the engine preference, and the tenant. It is
tenant-gated exactly like every other agent read (a non-member 404s)."""
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


def test_runtime_serves_repo_pointer_secrets_engine_and_tenant(client, workspace):
    Agent.objects.create(
        slug="echo", name="Echo", workspace=workspace,
        repo_url="https://github.com/dimagi/echo", repo_ref="main",
        runtime_engine=Agent.CLOUD_P, runtime_secrets=["canopy-pat", "echo-gog"],
    )
    resp = client.get("/api/agents/echo/runtime")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "slug": "echo",
        "repo_url": "https://github.com/dimagi/echo",
        "repo_ref": "main",
        "engine": "cloud_p",
        "secret_refs": ["canopy-pat", "echo-gog"],
        "workspace": "canopy",
    }


def test_runtime_defaults_for_an_unconfigured_agent(client, workspace):
    # Existing agents (pre-migration) carry empty runtime fields — the endpoint
    # still answers, so a runner learns "nothing declared yet" rather than 500ing.
    Agent.objects.create(slug="plain", name="Plain", workspace=workspace)
    body = client.get("/api/agents/plain/runtime").json()
    assert body["repo_url"] == ""
    assert body["repo_ref"] == "main"
    assert body["engine"] == "any"
    assert body["secret_refs"] == []


def test_runtime_is_tenant_gated(client, workspace):
    # An agent in a workspace the caller doesn't belong to 404s (no existence leak).
    other_owner = User.objects.create_user("other", "other@dimagi.com", "pw")
    other_ws = Workspace.objects.create(slug="other", display_name="Other", created_by=other_owner)
    WorkspaceMembership.objects.create(user=other_owner, workspace=other_ws, role=WorkspaceMembership.OWNER)
    Agent.objects.create(slug="secret-agent", name="Secret", workspace=other_ws)

    assert client.get("/api/agents/secret-agent/runtime").status_code == 404


def test_runtime_404_for_unknown_agent(client, workspace):
    assert client.get("/api/agents/nope/runtime").status_code == 404


def test_upsert_sets_runtime_fields(client, workspace):
    resp = client.post(
        "/api/agents/",
        data={
            "slug": "echo", "name": "Echo", "workspace": workspace.slug,
            "repo_url": "https://github.com/dimagi/echo",
            "runtime_engine": "cloud_p", "runtime_secrets": ["canopy-pat"],
        },
        content_type="application/json",
    )
    assert resp.status_code in (200, 201), resp.content
    body = client.get("/api/agents/echo/runtime").json()
    assert body["repo_url"] == "https://github.com/dimagi/echo"
    assert body["engine"] == "cloud_p"
    assert body["secret_refs"] == ["canopy-pat"]


def test_reupsert_without_runtime_fields_does_not_clobber(client, workspace):
    # The plugin re-upserts agents on every sync WITHOUT runtime fields; that must
    # not reset a previously-configured repo pointer / engine / secret refs.
    Agent.objects.create(
        slug="echo", name="Echo", workspace=workspace,
        repo_url="https://github.com/dimagi/echo", runtime_engine=Agent.CLOUD_P,
        runtime_secrets=["canopy-pat"],
    )
    resp = client.post(
        "/api/agents/",
        data={"slug": "echo", "name": "Echo (resynced)"},
        content_type="application/json",
    )
    assert resp.status_code in (200, 201), resp.content
    body = client.get("/api/agents/echo/runtime").json()
    assert body["repo_url"] == "https://github.com/dimagi/echo"  # preserved
    assert body["engine"] == "cloud_p"
    assert body["secret_refs"] == ["canopy-pat"]


def test_set_runner_preference(client, workspace):
    Agent.objects.create(slug="echo", name="Echo", workspace=workspace)
    resp = client.patch(
        "/api/agents/echo/runner-preference",
        data={"runner_preference": ["cloud", "emdash"]},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["runner_preference"] == ["cloud", "emdash"]
    Agent.objects.get(slug="echo").runner_preference == ["cloud", "emdash"]


def test_set_runner_preference_rejects_unknown_kind(client, workspace):
    Agent.objects.create(slug="echo", name="Echo", workspace=workspace)
    resp = client.patch(
        "/api/agents/echo/runner-preference",
        data={"runner_preference": ["cloud", "banana"]},
        content_type="application/json",
    )
    assert resp.status_code == 422
    assert Agent.objects.get(slug="echo").runner_preference == []  # unchanged
