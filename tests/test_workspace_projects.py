"""Workspace projects — owned by a workspace, kept current by a steward agent.

An agent project is one agent's piece of work with an end. A workspace project
is a standing resource many agents share (a funder knowledge base): addressed
`<workspace>/<handle>`, any agent in the workspace files tasks into it, and its
steward can change without its address changing.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentProject, AgentTask
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

J = "application/json"


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def ws(owner):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def eva(ws, owner):
    return Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=owner)


@pytest.fixture()
def ace(ws, owner):
    return Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner)


@pytest.fixture()
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


def _agent_project(client, slug="eva", **body):
    body.setdefault("name", "GiveWell Knowledge Base")
    r = client.post(f"/api/agents/{slug}/projects/", body, content_type=J)
    assert r.status_code == 201, r.content
    return r.json()


def _promote(client, ref="P1", handle="givewell-kb", slug="eva"):
    return client.patch(f"/api/agents/{slug}/projects/{ref}/", {"handle": handle}, content_type=J)


def test_promoting_gives_an_agent_project_a_workspace_address(client, eva):
    _agent_project(client)
    r = _promote(client)

    assert r.status_code == 200, r.content
    body = r.json()
    assert (body["scope"], body["handle"], body["steward_slug"]) == ("workspace", "givewell-kb", "eva")
    assert body["address"] == "dimagi/givewell-kb"
    assert body["url"] == "/w/dimagi/projects/givewell-kb"
    assert body["ext_id"] == "P1"  # its old address still works too
    assert client.get("/api/agents/eva/projects/P1/").status_code == 200


def test_an_agent_project_reports_its_own_scope(client, eva):
    body = _agent_project(client)
    assert (body["scope"], body["handle"], body["steward_slug"], body["address"]) == (
        "agent", "", None, "eva/P1")


def test_a_workspace_project_is_fetched_by_its_address(client, eva):
    _agent_project(client)
    _promote(client)

    r = client.get("/api/workspace-projects/dimagi/givewell-kb/")

    assert r.status_code == 200, r.content
    assert r.json()["name"] == "GiveWell Knowledge Base"
    assert client.get("/api/workspace-projects/dimagi/nope/").status_code == 404


def test_it_can_be_created_directly_with_a_steward(client, eva):
    r = client.post("/api/workspace-projects/", {"name": "Gates KB", "handle": "gates-kb", "steward": "eva"},
                    content_type=J)

    assert r.status_code == 201, r.content
    assert r.json()["address"] == "dimagi/gates-kb"
    assert AgentProject.objects.get(handle="gates-kb").agent == eva


def test_handles_are_unique_in_a_workspace_and_well_formed(client, eva, ace):
    _agent_project(client)
    _promote(client)
    _agent_project(client, slug="ace", name="Another")

    taken = _promote(client, slug="ace", handle="givewell-kb")
    bad = _promote(client, slug="ace", handle="Not A Handle!")

    assert taken.status_code == 409
    assert bad.status_code == 422


def test_a_handle_is_not_renamed_once_others_hold_it(client, eva):
    _agent_project(client)
    _promote(client)
    assert _promote(client, handle="givewell-kb").status_code == 200  # idempotent
    assert _promote(client, handle="givewell").status_code == 409


def test_any_agent_in_the_workspace_files_tasks_into_it_by_handle(client, eva, ace):
    _agent_project(client)
    _promote(client)

    r = client.post("/api/agents/ace/tasks/",
                    [{"ext_id": "T1", "title": "Pull the SMC parameters", "project": "givewell-kb"}],
                    content_type=J)

    assert r.status_code == 201, r.content
    assert r.json()[0]["project_address"] == "dimagi/givewell-kb"
    detail = client.get("/api/workspace-projects/dimagi/givewell-kb/").json()
    assert [(t["agent_slug"], t["ext_id"]) for t in detail["tasks"]] == [("ace", "T1")]
    assert detail["task_count"] == 1


def test_an_agent_project_stays_private_to_its_agent(client, eva, ace):
    _agent_project(client)  # eva/P1, not promoted
    r = client.post("/api/agents/ace/tasks/", [{"ext_id": "T1", "title": "x", "project": "eva/P1"}],
                    content_type=J)
    assert r.status_code == 201
    assert r.json()[0]["project_address"] is None


def test_the_steward_changes_and_the_address_does_not(client, eva, ace):
    _agent_project(client)
    _promote(client)
    _agent_project(client, slug="ace", name="ACE's own")  # ace already has a P1

    r = client.patch("/api/workspace-projects/dimagi/givewell-kb/", {"steward": "ace"}, content_type=J)

    assert r.status_code == 200, r.content
    body = r.json()
    assert (body["steward_slug"], body["address"]) == ("ace", "dimagi/givewell-kb")
    assert body["ext_id"] == "P2"  # re-issued from ace's counter: its P1 is taken


def test_the_steward_must_be_in_the_projects_workspace(client, eva, owner):
    other = Workspace.objects.create(slug="elsewhere", display_name="Else", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=other, role=WorkspaceMembership.OWNER)
    Agent.objects.create(slug="zed", name="Zed", workspace=other, owner=owner)
    _agent_project(client)
    _promote(client)

    r = client.patch("/api/workspace-projects/dimagi/givewell-kb/", {"steward": "zed"}, content_type=J)

    assert r.status_code == 422


def test_only_a_workspace_project_has_a_steward(client, eva, ace):
    _agent_project(client)
    r = client.patch("/api/agents/eva/projects/P1/", {"steward": "ace"}, content_type=J)
    assert r.status_code == 422


def test_its_steward_cannot_be_deleted_or_moved_out_from_under_it(client, eva, owner):
    _agent_project(client)
    _promote(client)
    dest = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=dest, role=WorkspaceMembership.OWNER)

    deleted = client.delete("/api/agents/eva/")
    moved = client.post("/api/agents/eva/move", {"workspace": "connect"}, content_type=J)

    assert deleted.status_code == 409
    assert "dimagi/givewell-kb" in deleted.json()["detail"]
    assert moved.status_code == 409, moved.content
    assert Agent.objects.filter(slug="eva", workspace_id="dimagi").exists()


def test_strangers_see_nothing_and_viewers_cannot_reshape(client, eva):
    _agent_project(client)
    _promote(client)
    stranger = User.objects.create_user("s", "s@dimagi.com", "pw")
    sc = Client()
    sc.force_login(stranger)
    assert sc.get("/api/workspace-projects/dimagi/givewell-kb/").status_code == 404
    assert sc.get("/api/workspace-projects/").json() == []

    viewer = User.objects.create_user("v", "v@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=eva.workspace, role=WorkspaceMembership.VIEWER)
    vc = Client()
    vc.force_login(viewer)
    assert vc.get("/api/workspace-projects/dimagi/givewell-kb/").status_code == 200
    assert vc.patch("/api/workspace-projects/dimagi/givewell-kb/", {"notes": "x"},
                    content_type=J).status_code == 403


def test_lists_filter_by_scope(client, eva):
    _agent_project(client)
    _agent_project(client, name="Agent-only work")
    _promote(client)

    ws_list = client.get("/api/workspace-projects/").json()
    fleet_ws = client.get("/api/projects/?scope=workspace").json()
    fleet_agent = client.get("/api/projects/?scope=agent").json()

    assert [p["address"] for p in ws_list] == ["dimagi/givewell-kb"]
    assert [p["address"] for p in fleet_ws] == ["dimagi/givewell-kb"]
    assert [p["address"] for p in fleet_agent] == ["eva/P2"]


def test_a_half_workspace_project_is_refused_by_the_database(eva):
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        AgentProject.objects.create(agent=eva, ext_id="P9", name="x", scope="workspace", handle="")
    with pytest.raises(IntegrityError), transaction.atomic():
        AgentProject.objects.create(agent=eva, ext_id="P10", name="y", scope="agent", handle="stray")


def test_its_counts_include_other_agents_tasks_on_the_stewards_board(client, eva, ace):
    _agent_project(client)
    _promote(client)
    AgentTask.objects.create(agent=ace, ext_id="T1", title="ACE's",
                             project=AgentProject.objects.get(handle="givewell-kb"))

    row = client.get("/api/agents/eva/projects/").json()[0]

    assert row["task_count"] == 1
