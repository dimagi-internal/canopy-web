"""Projects are the workspace's (design 2026-10-24): addressed `<workspace>/<handle>`,
led by a person and/or a lead agent (which holds the Drive folder), worked on by
member agents, and any agent of the workspace files tasks into one. An agent's
route still names a project by the `P<n>` alias it was made with.
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


def test_every_project_has_a_workspace_address(client, eva):
    body = _agent_project(client)
    assert (body["handle"], body["steward_slug"], body["address"]) == (
        "givewell-knowledge-base", "eva", "dimagi/givewell-knowledge-base")
    assert body["url"] == body["board_url"] == "/w/dimagi/projects/givewell-knowledge-base"
    assert body["ext_id"] == "P1" and body["member_agents"] == ["eva"]
    assert client.get("/api/agents/eva/projects/P1/").status_code == 200
    assert client.get("/api/workspace-projects/dimagi/givewell-knowledge-base/").status_code == 200


def test_readdressing_gives_it_a_new_handle_and_keeps_its_alias(client, eva):
    _agent_project(client)
    r = _promote(client)

    assert r.status_code == 200, r.content
    assert (r.json()["address"], r.json()["ext_id"]) == ("dimagi/givewell-kb", "P1")
    assert _promote(client).status_code == 200  # the same address: a no-op
    assert client.get("/api/workspace-projects/dimagi/givewell-kb/").json()["name"] == "GiveWell Knowledge Base"
    assert client.get("/api/workspace-projects/dimagi/nope/").status_code == 404


def test_it_can_be_created_directly_with_a_lead_agent_or_none(client, eva, ace):
    led = client.post("/api/workspace-projects/", {"name": "Gates KB", "handle": "gates-kb", "steward": "eva",
                                                    "member_agents": ["ace"]}, content_type=J)
    unled = client.post("/api/workspace-projects/", {"name": "Offsite", "workspace": "dimagi"}, content_type=J)

    assert led.status_code == 201, led.content
    assert (led.json()["address"], led.json()["member_agents"]) == ("dimagi/gates-kb", ["ace", "eva"])
    assert AgentProject.objects.get(handle="gates-kb").agent == eva
    assert unled.status_code == 201, unled.content
    assert (unled.json()["address"], unled.json()["agent_slug"], unled.json()["ext_id"]) == (
        "dimagi/offsite", None, "")


def test_handles_are_unique_in_a_workspace_and_well_formed(client, eva, ace):
    _agent_project(client)
    _promote(client)
    _agent_project(client, slug="ace", name="Another")

    taken = _promote(client, slug="ace", handle="givewell-kb")
    bad = _promote(client, slug="ace", handle="Not A Handle!")

    assert taken.status_code == 409
    assert bad.status_code == 422


def test_a_second_project_of_the_same_name_gets_its_own_handle(client, eva, ace):
    one = _agent_project(client)
    two = _agent_project(client, slug="ace")
    assert (one["handle"], two["handle"]) == ("givewell-knowledge-base", "givewell-knowledge-base-2")


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


def test_the_lead_agent_changes_and_the_address_and_alias_do_not(client, eva, ace):
    _agent_project(client)
    _promote(client)
    _agent_project(client, slug="ace", name="ACE's own")  # ace already has a P1

    r = client.patch("/api/workspace-projects/dimagi/givewell-kb/", {"steward": "ace"}, content_type=J)

    assert r.status_code == 200, r.content
    body = r.json()
    assert (body["steward_slug"], body["address"], body["ext_id"]) == ("ace", "dimagi/givewell-kb", "P1")
    assert body["member_agents"] == ["ace", "eva"]
    # The alias is still eva's to name it by; ace's own P1 is another project.
    assert client.get("/api/agents/eva/projects/P1/").json()["handle"] == "givewell-kb"
    assert client.get("/api/agents/ace/projects/P1/").json()["name"] == "ACE's own"
    cleared = client.patch("/api/workspace-projects/dimagi/givewell-kb/", {"steward": ""}, content_type=J)
    assert cleared.json()["agent_slug"] is None


def test_the_lead_agent_must_be_in_the_projects_workspace(client, eva, owner):
    other = Workspace.objects.create(slug="elsewhere", display_name="Else", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=other, role=WorkspaceMembership.OWNER)
    Agent.objects.create(slug="zed", name="Zed", workspace=other, owner=owner)
    _agent_project(client)
    _promote(client)

    r = client.patch("/api/workspace-projects/dimagi/givewell-kb/", {"steward": "zed"}, content_type=J)

    assert r.status_code == 422


def test_deleting_its_lead_agent_leaves_the_project(client, eva):
    _agent_project(client)
    _promote(client)

    deleted = client.delete("/api/agents/eva/")

    assert deleted.status_code == 204, deleted.content
    project = AgentProject.objects.get(handle="givewell-kb")
    assert project.agent is None and project.workspace_id == "dimagi"


def test_a_proposed_project_is_not_started_by_changing_its_status(client, eva):
    project = AgentProject.objects.create(agent=eva, ext_id="P1", name="Idea", status=AgentProject.PROPOSED)

    started = client.patch(f"/api/workspace-projects/dimagi/{project.handle}/", {"status": "active"},
                           content_type=J)
    canceled = client.patch(f"/api/workspace-projects/dimagi/{project.handle}/", {"status": "canceled"},
                            content_type=J)

    assert started.status_code == 409, started.content
    assert canceled.status_code == 200 and canceled.json()["status"] == "canceled"


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
    seen = vc.get("/api/workspace-projects/dimagi/givewell-kb/")
    assert seen.status_code == 200 and seen.json()["may_edit"] is False
    assert vc.patch("/api/workspace-projects/dimagi/givewell-kb/", {"notes": "x"},
                    content_type=J).status_code == 403


def test_its_lead_person_may_change_it(client, eva):
    lead = User.objects.create_user("l", "l@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=lead, workspace=eva.workspace, role=WorkspaceMembership.VIEWER)
    _agent_project(client, owner_email="l@dimagi.com")
    lc = Client()
    lc.force_login(lead)

    r = lc.patch("/api/agents/eva/projects/P1/", {"target_date": "2026-12-01", "priority": 2}, content_type=J)

    assert r.status_code == 200, r.content
    assert (r.json()["target_date"], r.json()["priority"]) == ("2026-12-01", 2)


def test_every_project_is_listed_whatever_scope_asks(client, eva):
    _agent_project(client)
    _agent_project(client, name="Agent-only work")

    ws_list = client.get("/api/workspace-projects/").json()
    fleet = client.get("/api/projects/?scope=agent").json()

    assert sorted(p["address"] for p in ws_list) == ["dimagi/agent-only-work", "dimagi/givewell-knowledge-base"]
    assert sorted(p["address"] for p in fleet) == sorted(p["address"] for p in ws_list)


def test_two_projects_cannot_share_an_address(eva):
    from django.db import IntegrityError, transaction

    AgentProject.objects.create(agent=eva, ext_id="P9", name="x", handle="same")
    with pytest.raises(IntegrityError), transaction.atomic():
        AgentProject.objects.create(agent=eva, ext_id="P10", name="y", handle="same")


def test_its_counts_include_every_agents_tasks(client, eva, ace):
    _agent_project(client)
    _promote(client)
    AgentTask.objects.create(agent=ace, ext_id="T1", title="ACE's",
                             project=AgentProject.objects.get(handle="givewell-kb"))

    row = client.get("/api/agents/eva/projects/").json()[0]

    assert row["task_count"] == 1


# ---- a project of a workspace below its steward's (dimagi's Eva keeps connect's KB) ----


@pytest.fixture()
def connect(ws, owner):
    return Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner, parent=ws)


@pytest.fixture()
def ace_in_connect(connect, owner):
    return Agent.objects.create(slug="ace", name="ACE", workspace=connect, owner=owner)


def _promote_into_connect(client):
    return client.patch("/api/agents/eva/projects/P1/", {"handle": "givewell-kb", "workspace": "connect"},
                        content_type=J)


def test_a_parent_workspaces_agent_stewards_a_project_of_the_division(client, eva, connect):
    _agent_project(client)
    r = _promote_into_connect(client)

    assert r.status_code == 200, r.content
    body = r.json()
    assert (body["address"], body["steward_slug"], body["url"]) == (
        "connect/givewell-kb", "eva", "/w/connect/projects/givewell-kb")
    assert body["board_url"] == body["url"]  # one page
    assert client.get("/api/workspace-projects/connect/givewell-kb/").json()["board_readable"] is True


def test_it_can_be_created_directly_in_the_division(client, eva, connect):
    r = client.post("/api/workspace-projects/",
                    {"name": "Gates KB", "handle": "gates-kb", "steward": "eva", "workspace": "connect"},
                    content_type=J)
    assert r.status_code == 201, r.content
    assert r.json()["address"] == "connect/gates-kb"


def test_a_steward_cannot_hold_a_project_above_or_beside_its_workspace(client, connect, ace_in_connect, owner):
    other = Workspace.objects.create(slug="elsewhere", display_name="Else", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=other, role=WorkspaceMembership.OWNER)
    _agent_project(client, slug="ace")

    up = client.patch("/api/agents/ace/projects/P1/", {"handle": "kb", "workspace": "dimagi"}, content_type=J)
    across = client.patch("/api/agents/ace/projects/P1/", {"handle": "kb", "workspace": "elsewhere"},
                          content_type=J)

    assert up.status_code == 422, up.content
    assert across.status_code == 422, across.content


def test_placing_it_in_another_workspace_needs_an_editor_there(eva, connect):
    # A dimagi editor who is nobody in connect writes Eva, but cannot make connect own a project.
    editor = User.objects.create_user("ed", "ed@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=editor, workspace=eva.workspace, role=WorkspaceMembership.EDITOR)
    ec = Client()
    ec.force_login(editor)
    _agent_project(ec)

    r = _promote_into_connect(ec)

    assert r.status_code == 404, r.content
    assert AgentProject.objects.get(ext_id="P1").workspace_id == "dimagi"


def test_the_divisions_agents_file_into_it_by_handle_and_the_steward_by_address(client, eva, connect,
                                                                                ace_in_connect):
    _agent_project(client)
    _promote_into_connect(client)

    by_ace = client.post("/api/agents/ace/tasks/",
                         [{"ext_id": "T1", "title": "Pull the SMC parameters", "project": "givewell-kb"}],
                         content_type=J)
    by_eva = client.post("/api/agents/eva/tasks/",
                         [{"ext_id": "T1", "title": "Weekly refresh", "project": "connect/givewell-kb"}],
                         content_type=J)

    assert by_ace.json()[0]["project_address"] == "connect/givewell-kb", by_ace.content
    assert by_eva.json()[0]["project_address"] == "connect/givewell-kb", by_eva.content


def test_an_org_agent_cannot_reach_a_divisions_project_by_bare_handle(client, eva, connect, ace_in_connect):
    # A bare handle looks in the agent's workspace and UP its tree, never down:
    # "givewell-kb" from a dimagi agent must not silently mean connect's.
    client.post("/api/workspace-projects/",
                {"name": "KB", "handle": "givewell-kb", "steward": "ace"}, content_type=J)
    Agent.objects.create(slug="ada", name="Ada", workspace=eva.workspace, owner=eva.owner)

    r = client.post("/api/agents/ada/tasks/", [{"ext_id": "T1", "title": "x", "project": "givewell-kb"}],
                    content_type=J)

    assert r.status_code == 201, r.content
    assert r.json()[0]["project_address"] is None


def test_the_steward_reads_it_though_it_is_no_member_of_the_division(eva, connect):
    # Eva's own login is a dimagi member only; the tree withholds connect from it.
    eva_user = User.objects.create_user("eva-bot", "eva@agents.test", "pw")
    WorkspaceMembership.objects.create(user=eva_user, workspace=eva.workspace, role=WorkspaceMembership.EDITOR)
    ec = Client()
    ec.force_login(eva_user)
    AgentProject.objects.create(agent=eva, ext_id="P1", name="GiveWell Knowledge Base",
                                 workspace=connect, handle="givewell-kb")

    detail = ec.get("/api/workspace-projects/connect/givewell-kb/")
    listed = ec.get("/api/workspace-projects/").json()

    assert detail.status_code == 200, detail.content
    assert [p["address"] for p in listed] == ["connect/givewell-kb"]


def test_a_division_member_reads_it_without_seeing_the_parent(eva, connect):
    member = User.objects.create_user("c", "c@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=member, workspace=connect, role=WorkspaceMembership.VIEWER)
    mc = Client()
    mc.force_login(member)
    AgentProject.objects.create(agent=eva, ext_id="P1", name="KB", 
                                workspace=connect, handle="givewell-kb")

    detail = mc.get("/api/workspace-projects/connect/givewell-kb/")
    assert detail.status_code == 200
    assert detail.json()["board_readable"] is True  # its page is the project's own, in connect
    assert mc.get("/api/agents/eva/").status_code == 404


def test_a_parent_agent_leading_a_division_project_cannot_be_moved_out_of_the_tree(client, eva, connect, owner):
    _agent_project(client)
    _promote_into_connect(client)
    other = Workspace.objects.create(slug="elsewhere", display_name="Else", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=other, role=WorkspaceMembership.OWNER)

    moved = client.post("/api/agents/eva/move", {"workspace": "elsewhere"}, content_type=J)

    assert moved.status_code == 409, moved.content
    assert "connect/givewell-kb" in moved.json()["detail"]
