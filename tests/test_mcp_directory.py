"""The MCP directory: the project servers you could add, for a workspace and the ones below it.

A list to browse, never a subscription. Every row it shows is a server its reader
can actually call (`apps.mcp.projects.readable_project`), and it shows nothing
from a workspace the reader is not in.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentProject, ProjectService
from apps.mcp.projects import readable_project
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _served(agent, workspace, handle, **kw):
    p = AgentProject.objects.create(agent=agent, ext_id=f"P{AgentProject.objects.count() + 1}", name=handle.title(),
                                    workspace=workspace, handle=handle, **kw)
    ProjectService.objects.create(project=p, kind="kb", name="kb",
                                  kb_key=f"workspace-projects/{workspace.slug}/{handle}/kb")
    return p


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner, parent=dimagi)
    other = Workspace.objects.create(slug="acme", display_name="Acme", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=dimagi, role=WorkspaceMembership.OWNER)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=dimagi, owner=owner)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=connect, owner=owner)
    wile = Agent.objects.create(slug="wile", name="Wile", workspace=other, owner=owner)
    gw = _served(eva, connect, "givewell-kb", outcome="Everything GiveWell has published.")
    _served(eva, dimagi, "policies-kb")
    _served(ace, connect, "old-kb", status=AgentProject.ARCHIVED)
    _served(wile, other, "acme-kb")
    draft = AgentProject.objects.create(agent=ace, ext_id="P99", name="Not yet", workspace=connect, handle="draft-kb")
    ProjectService.objects.create(project=draft, kind="kb", name="kb")  # nothing published
    return {"owner": owner, "dimagi": dimagi, "connect": connect, "gw": gw}


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _names(resp) -> list[str]:
    assert resp.status_code == 200, resp.content
    return sorted(r["address"] for r in resp.json())


def test_a_workspace_lists_its_own_servers_and_those_below_it(world):
    resp = _client(world["owner"]).get("/api/w/dimagi/mcp-servers/")
    assert _names(resp) == ["connect/givewell-kb", "dimagi/policies-kb"]
    row = next(r for r in resp.json() if r["name"] == "givewell-kb")
    assert row["workspace_name"] == "Connect" and row["steward_slug"] == "eva" and row["kind"] == "kb"
    assert row["description"] == "Everything GiveWell has published."
    assert row["url"].endswith("/api/mcp/projects/connect/givewell-kb")
    assert row["add_command"] == f"claude mcp add --transport http givewell-kb {row['url']}"
    assert row["project_url"] == "/w/connect/projects/givewell-kb"


def test_a_division_does_not_list_its_parents_servers(world):
    assert _names(_client(world["owner"]).get("/api/w/connect/mcp-servers/")) == ["connect/givewell-kb"]


def test_a_division_member_sees_only_their_division(world):
    sam = User.objects.create_user("sam", "sam@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=sam, workspace=world["connect"], role=WorkspaceMembership.EDITOR)
    assert _names(_client(sam).get("/api/w/connect/mcp-servers/")) == ["connect/givewell-kb"]
    assert _client(sam).get("/api/w/dimagi/mcp-servers/").status_code in (403, 404)


def test_every_listed_server_is_one_its_reader_can_call(world):
    """The parent's viewer is not in the division, yet reads the KB its parent's agent keeps:
    the server admits the steward's workspace, so the directory lists it too."""
    vic = User.objects.create_user("vic", "vic@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=vic, workspace=world["dimagi"], role=WorkspaceMembership.VIEWER)
    rows = _client(vic).get("/api/w/dimagi/mcp-servers/").json()
    assert sorted(r["address"] for r in rows) == ["connect/givewell-kb", "dimagi/policies-kb"]
    for r in rows:
        assert readable_project(vic, r["workspace"], r["name"]) is not None
