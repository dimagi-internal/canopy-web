"""A project shared across its workspace tree (`ProjectShare`): its members and readers
may come from the workspaces it is shared with and the ones below them, never from
another organisation.

The case that shaped it: `dimagi/canopy-support` carries skills for the whole
organisation, and hal, an agent of the division `connect`, should join it and read them.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents import services
from apps.agents.models import Agent, AgentProject, AgentTask, ProjectService, ProjectShare
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

J = "application/json"
ADDR = "/api/workspace-projects/dimagi/canopy-support/"
SHARES = ADDR + "shares/"


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _member(ws, email, role):
    u = User.objects.create_user(email.split("@")[0], email, "pw")
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=role)
    return u


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner, parent=dimagi)
    team = Workspace.objects.create(slug="connect-team", display_name="Team", created_by=owner, parent=connect)
    labs = Workspace.objects.create(slug="labs", display_name="Labs", created_by=owner, parent=dimagi)
    elsewhere = User.objects.create_user("el", "el@else.com", "pw")
    other = Workspace.objects.create(slug="outsider", display_name="Outsider", created_by=elsewhere)
    WorkspaceMembership.objects.create(user=owner, workspace=dimagi, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=elsewhere, workspace=other, role=WorkspaceMembership.OWNER)
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=dimagi, owner=owner)
    hal_login = _member(connect, "hal@agents.test", WorkspaceMembership.EDITOR)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=connect, owner=owner, user=hal_login)
    deep = Agent.objects.create(slug="deep", name="Deep", workspace=team, owner=owner)
    lab = Agent.objects.create(slug="lab", name="Lab", workspace=labs, owner=owner)
    stranger = Agent.objects.create(slug="stranger", name="Stranger", workspace=other, owner=owner)
    project = AgentProject.objects.create(agent=ada, ext_id="P1", name="Canopy support", workspace=dimagi,
                                          handle="canopy-support")
    ProjectService.objects.create(project=project, kind="skill", name="admin-who-may-ask",
                                  description="Who may ask", instructions="Check the roles first.")
    return {"owner": owner, "client": _client(owner), "dimagi": dimagi, "connect": connect, "team": team,
            "labs": labs, "other": other, "ada": ada, "hal": hal, "hal_login": hal_login, "deep": deep,
            "lab": lab, "stranger": stranger, "project": project}


def _members(client, agents, url=ADDR):
    return client.patch(url, {"member_agents": agents}, content_type=J)


def _share(client, ws, url=SHARES):
    return client.post(url, {"workspace": ws}, content_type=J)


# ---- joining ----


def test_a_division_agent_joins_a_project_shared_with_its_workspace(world):
    c = world["client"]
    assert _members(c, ["ada", "hal"]).status_code == 422  # not before the share

    assert _share(c, "connect").status_code == 201
    r = _members(c, ["ada", "hal"])

    assert r.status_code == 200, r.content
    assert r.json()["member_agents"] == ["ada", "hal"]
    assert r.json()["shared_with"] == ["connect"]


def test_the_share_covers_the_workspaces_below_it_and_not_a_sibling(world):
    c = world["client"]
    _share(c, "connect")

    assert _members(c, ["ada", "deep"]).status_code == 200  # connect-team is below connect
    assert _members(c, ["ada", "lab"]).status_code == 422  # labs is beside connect


def test_it_can_be_created_with_an_agent_from_a_shared_workspace_only_after_the_share(world):
    c = world["client"]
    body = {"name": "Other", "handle": "other-proj", "workspace": "dimagi", "member_agents": ["hal"]}
    assert c.post("/api/workspace-projects/", body, content_type=J).status_code == 422
    assert not AgentProject.objects.filter(handle="other-proj").exists()  # nothing half-made


def test_an_agent_of_another_organisation_is_never_a_member(world):
    c = world["client"]
    assert _share(c, "outsider").status_code == 422
    r = _members(c, ["ada", "stranger"])
    assert r.status_code == 404  # no oracle over another organisation's agents
    assert not world["project"].member_agents.filter(slug="stranger").exists()


def test_leading_the_project_still_needs_its_own_side_of_the_tree(world):
    c = world["client"]
    _share(c, "connect")
    r = c.patch(ADDR, {"steward": "hal"}, content_type=J)
    assert r.status_code in (404, 422)
    world["project"].refresh_from_db()
    assert world["project"].agent == world["ada"]


def test_the_proposals_path_applies_the_same_rule(world):
    from apps.agents import proposals

    project = world["project"]
    assert proposals._add_member(project, world["hal"]) == ""
    _share(world["client"], "connect")
    assert proposals._add_member(project, world["hal"]) == "dimagi/canopy-support"
    assert project.member_agents.filter(slug="hal").exists()


def test_a_member_agent_finds_a_siblings_project_by_its_address(world):
    # Down the tree an address already resolves (a division's agent uses the org's
    # projects); a project of a SIBLING workspace needs the share.
    hal, c = world["hal"], world["client"]
    sibling = AgentProject.objects.create(name="Labs work", workspace=world["labs"], handle="labs-work")
    url = "/api/workspace-projects/labs/labs-work/shares/"
    assert services.get_project(hal, "labs/labs-work") is None

    assert _share(c, "connect", url).status_code == 201
    assert services.get_project(hal, "labs/labs-work") == sibling
    assert services.get_project(world["deep"], "labs/labs-work") == sibling  # below connect
    assert services.get_project(world["ada"], "labs/labs-work") == sibling  # above labs: as before
    assert services.get_project(world["stranger"], "labs/labs-work") is None
    assert services.get_project(hal, "connect/labs-work") is None


def test_a_task_of_a_shared_project_can_be_assigned_to_a_member_agent(world):
    project, hal = world["project"], world["hal"]
    with pytest.raises(services.AssigneeError):
        services.resolve_assignee("dimagi", "hal", project)
    _share(world["client"], "connect")

    agent, _ = services.resolve_assignee("dimagi", "hal", project)
    assert agent == hal
    with pytest.raises(services.AssigneeError):  # without the project in hand it is still dimagi's side only
        services.resolve_assignee("dimagi", "hal")

    task = services.create_tasks(None, [{"title": "Join", "project": "dimagi/canopy-support",
                                        "assignee": "hal"}], workspace="dimagi", by_user=world["owner"])[0]
    assert task.agent == hal and task.project == project


# ---- who may share ----


def test_only_an_editor_of_the_projects_workspace_may_share(world):
    viewer = _member(world["dimagi"], "v@dimagi.com", WorkspaceMembership.VIEWER)
    editor = _member(world["dimagi"], "e@dimagi.com", WorkspaceMembership.EDITOR)

    assert _share(_client(viewer), "connect").status_code == 403
    assert _client(viewer).delete(SHARES + "connect/").status_code in (403, 404)
    assert _share(_client(editor), "connect").status_code == 201
    assert _client(viewer).delete(SHARES + "connect/").status_code == 403
    assert ProjectShare.objects.count() == 1
    assert _client(viewer).get(SHARES).status_code == 200  # a reader may see how it is shared


def test_a_stranger_cannot_even_see_that_it_exists(world):
    stranger = User.objects.create_user("x", "x@else.com", "pw")
    assert _share(_client(stranger), "connect").status_code == 404
    assert _client(stranger).get(SHARES).status_code == 404


def test_an_agent_that_works_on_it_cannot_widen_who_reads_it(world):
    _share(world["client"], "connect")
    _members(world["client"], ["ada", "hal"])
    assert _share(_client(world["hal_login"]), "labs").status_code == 403


# ---- what may be shared ----


def test_it_is_shared_only_inside_its_tree_and_not_with_itself_or_above(world):
    c = world["client"]
    assert _share(c, "outsider").status_code == 422
    assert _share(c, "dimagi").status_code == 422  # its own workspace
    assert _share(c, "nowhere").status_code == 404
    deep = AgentProject.objects.create(name="Deep", workspace=world["team"], handle="deep-proj")
    assert _share(c, "connect", f"/api/workspace-projects/connect-team/{deep.handle}/shares/").status_code == 422  # above it
    assert _share(c, "connect", f"/api/workspace-projects/connect-team/{deep.handle}/shares/").status_code == 422
    assert ProjectShare.objects.count() == 0


def test_sharing_twice_is_one_share(world):
    c = world["client"]
    assert _share(c, "connect").status_code == 201
    assert _share(c, "connect").status_code == 201
    assert ProjectShare.objects.count() == 1
    listed = c.get(SHARES).json()
    assert [row["workspace"] for row in listed] == ["connect"]
    assert listed[0]["added_by"] == "jj@dimagi.com"


def test_the_listing_names_the_agents_that_joined_through_each_share(world):
    c = world["client"]
    _share(c, "connect")
    _share(c, "labs")
    _members(c, ["ada", "hal", "deep", "lab"])

    rows = {row["workspace"]: row["member_agents"] for row in c.get(SHARES).json()}
    assert rows == {"connect": ["deep", "hal"], "labs": ["lab"]}


# ---- revoking ----


def test_revoking_removes_the_members_it_let_in_and_never_the_lead(world):
    c = world["client"]
    _share(c, "connect")
    _share(c, "labs")
    _members(c, ["ada", "hal", "deep", "lab"])

    r = c.delete(SHARES + "connect/")

    assert r.status_code == 200, r.content
    assert r.json() == {"workspace": "connect", "removed_agents": ["deep", "hal"]}
    assert sorted(a.slug for a in world["project"].member_agents.all()) == ["ada", "lab"]
    assert c.delete(SHARES + "connect/").status_code == 404  # already gone


def test_another_share_that_still_covers_a_member_keeps_it(world):
    c = world["client"]
    _share(c, "connect")
    _share(c, "connect-team")
    _members(c, ["ada", "deep"])

    r = c.delete(SHARES + "connect/")

    assert r.json()["removed_agents"] == []  # connect-team still covers deep
    assert world["project"].member_agents.filter(slug="deep").exists()


def test_revoking_never_removes_the_lead_agent_or_one_of_the_own_side(world):
    c = world["client"]
    other = Agent.objects.create(slug="eva", name="Eva", workspace=world["dimagi"], owner=world["owner"])
    _share(c, "connect")
    _members(c, ["ada", "eva", "hal"])
    r = c.delete(SHARES + "connect/")
    assert r.json()["removed_agents"] == ["hal"]
    assert set(world["project"].member_agents.values_list("slug", flat=True)) == {"ada", other.slug}


# ---- reading ----


def test_a_member_of_a_shared_workspace_reads_the_project_and_its_tasks(world):
    reader = _member(world["connect"], "cm@dimagi.com", WorkspaceMembership.VIEWER)
    below = _member(world["team"], "tm@dimagi.com", WorkspaceMembership.VIEWER)
    sibling = _member(world["labs"], "lm@dimagi.com", WorkspaceMembership.VIEWER)
    task = AgentTask.objects.create(workspace=world["dimagi"], project=world["project"], title="Do it",
                                    number=1, key="DIM-1", agent=world["ada"])
    for who in (reader, below, sibling):
        assert _client(who).get(ADDR).status_code == 404
    assert _client(reader).get(f"/api/tasks/{task.key}/").status_code == 404

    _share(world["client"], "connect")

    assert _client(reader).get(ADDR).status_code == 200
    assert _client(below).get(ADDR).status_code == 200
    assert _client(sibling).get(ADDR).status_code == 404
    assert _client(reader).get(f"/api/tasks/{task.key}/").status_code == 200
    assert _client(sibling).get(f"/api/tasks/{task.key}/").status_code == 404
    assert _client(reader).get(ADDR).json()["may_edit"] is False


def test_a_list_shows_the_project_to_the_shared_workspace_marked_shared_in(world):
    reader = _member(world["connect"], "cm@dimagi.com", WorkspaceMembership.VIEWER)
    c = _client(reader)
    assert c.get("/api/workspace-projects/").json() == []

    _share(world["client"], "connect")
    rows = c.get("/api/workspace-projects/").json()

    assert [(r["address"], r["shared_in"], r["shared_with"]) for r in rows] == [
        ("dimagi/canopy-support", True, ["connect"])]
    own = world["client"].get("/api/workspace-projects/").json()
    assert [(r["address"], r["shared_in"]) for r in own] == [("dimagi/canopy-support", False)]


def test_a_member_agent_reads_the_project_through_its_own_route(world):
    sibling = AgentProject.objects.create(name="Labs work", workspace=world["labs"], handle="labs-work")
    _share(world["client"], "connect", "/api/workspace-projects/labs/labs-work/shares/")
    c = _client(world["hal_login"])
    assert c.get(f"/api/agents/hal/projects/{sibling.pk}/").status_code == 404  # shared, not yet a member

    r = _members(world["client"], ["hal"], "/api/workspace-projects/labs/labs-work/")
    assert r.status_code == 200, r.content

    assert c.get(f"/api/agents/hal/projects/{sibling.pk}/").status_code == 200
    assert c.get("/api/workspace-projects/labs/labs-work/").status_code == 200


# ---- moving an agent ----


def test_moving_a_member_agent_out_of_reach_drops_the_membership_and_says_so(world):
    from apps.agents import move

    _share(world["client"], "connect")
    _members(world["client"], ["ada", "hal"])
    plan = move.plan(world["hal"], world["labs"], by=world["owner"])
    kinds = {row["kind"] for row in plan.stops_working}
    assert "project_memberships" in kinds
    assert world["project"].member_agents.filter(slug="hal").exists()  # a dry run changes nothing

    move.move(world["hal"], world["labs"], by=world["owner"])
    assert not world["project"].member_agents.filter(slug="hal").exists()


def test_moving_within_the_shared_side_keeps_the_membership(world):
    from apps.agents import move

    _share(world["client"], "connect")
    _members(world["client"], ["ada", "deep"])
    plan = move.plan(world["deep"], world["connect"], by=world["owner"])  # connect-team -> connect: still shared
    assert "project_memberships" not in {row["kind"] for row in plan.stops_working}


# ---- the tree changes under a share ----


def test_a_share_that_would_straddle_two_organisations_is_ended_when_a_workspace_moves(world):
    _share(world["client"], "connect")
    connect = world["connect"]
    connect.parent = world["other"]
    connect.save()
    assert ProjectShare.objects.count() == 0
