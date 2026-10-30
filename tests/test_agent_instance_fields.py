"""An agent row is an INSTANCE: the agents API names its plugin.

Jonathan, 2026-10-14: a plugin is code; an instance is canopy-web's Agent record
(identity, workspace). A local install is bound to one instance, chosen once at setup.

What these pin:

- no 1Password field (`op_vault`, `credential_source`, a service-account token) is on
  an item of `GET /api/agents/` or on the single-agent GET;
- `plugin` is `{"repo", "name"}` — the name the caller's `canopy-agents` catalog
  uses, so the two cannot disagree — or null for an agent with no github.com repo.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from apps.agents.models import Agent
from apps.plugins import services as plugin_services
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_names():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def member():
    return User.objects.create_user("pat", "pat@dimagi.com", "pw")


@pytest.fixture()
def workspace(member):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=member)
    WorkspaceMembership.objects.create(user=member, workspace=ws, role=WorkspaceMembership.VIEWER)
    return ws


@pytest.fixture()
def client(member):
    c = Client()
    c.force_login(member)
    return c


@pytest.fixture()
def fleet(workspace):
    Agent.objects.create(slug="ace", name="ACE", workspace=workspace,
                         repo_url="git@github.com:dimagi-internal/ace.git")
    Agent.objects.create(slug="local", name="Local", workspace=workspace)   # no repo


def items(client) -> dict[str, dict]:
    return {a["slug"]: a for a in client.get("/api/agents/").json()["items"]}


def test_list_carries_the_plugin(client, fleet):
    agents = items(client)
    assert agents["ace"]["plugin"] == {"repo": "dimagi-internal/ace", "name": "ace"}
    assert agents["local"]["plugin"] is None


def test_detail_carries_the_plugin(client, fleet):
    body = client.get("/api/agents/ace/").json()
    assert body["plugin"] == {"repo": "dimagi-internal/ace", "name": "ace"}
    assert client.get("/api/agents/local/").json()["plugin"] is None


def test_no_1password_field_is_on_the_agent(client, fleet):
    for path in ("/api/agents/", "/api/agents/ace/"):
        text = client.get(path).content.decode()
        for gone in ("op_sa_token", "op_vault", "credential_source"):
            assert gone not in text


def test_the_plugin_name_is_the_catalogs(client, fleet, monkeypatch):
    """plugin.json's name wins over the repo name — in the catalog AND here."""
    class Found:
        status_code = 200

        @staticmethod
        def json():
            return {"name": "connect-ace"}

    monkeypatch.setattr(plugin_services, "_raw_get", lambda url: Found())
    assert items(client)["ace"]["plugin"]["name"] == "connect-ace"
    [entry] = plugin_services.catalog(User.objects.get(username="pat"))["plugins"]
    assert entry["name"] == "connect-ace"
