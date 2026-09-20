"""An agent's projects — the work its `Projects/<name>` Drive folder holds.

canopy keeps the state (status, owner, what is in flight); Drive keeps the
files. Per agent, matching the Drive layout: two agents on the same initiative
have a project each and share files when they want to.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentProject, AgentTask
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def agent(owner):
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=owner)


@pytest.fixture()
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


def _create(client, **body):
    body.setdefault("name", "UNGA 2026 conference planning")
    return client.post("/api/agents/eva/projects/", body, content_type="application/json")


def test_a_project_is_created_and_numbered_per_agent(client, agent):
    first = _create(client)
    second = _create(client, name="Coefficient Giving EOI")

    assert first.status_code == 201, first.content
    assert first.json()["ext_id"] == "P1"
    assert second.json()["ext_id"] == "P2"
    assert first.json()["status"] == "active"


def test_numbering_survives_a_deleted_project(client, agent):
    _create(client)
    _create(client, name="Second")
    AgentProject.objects.get(ext_id="P2").delete()

    third = _create(client, name="Third")

    # P3, not P2: a reused id would make an old link point at new work.
    assert third.json()["ext_id"] == "P3"


def test_it_carries_the_drive_folder_rather_than_replacing_it(client, agent):
    """The folder stays the home of the files; the project points at it."""
    r = _create(client, drive_folder_id="1AbC", drive_folder_url="https://drive.google.com/x")

    body = r.json()
    assert (body["drive_folder_id"], body["drive_folder_url"]) == (
        "1AbC", "https://drive.google.com/x")


def test_projects_are_per_agent_and_do_not_leak(client, agent, owner):
    Agent.objects.create(slug="hal", name="Hal", workspace=agent.workspace, owner=owner)
    _create(client)
    client.post("/api/agents/hal/projects/", {"name": "Hal's own"},
                content_type="application/json")

    eva = client.get("/api/agents/eva/projects/").json()
    hal = client.get("/api/agents/hal/projects/").json()

    assert [p["name"] for p in eva] == ["UNGA 2026 conference planning"]
    assert [p["name"] for p in hal] == ["Hal's own"]
    # Same numbering, different agents — the ext_id is only unique per agent.
    assert eva[0]["ext_id"] == hal[0]["ext_id"] == "P1"


def test_a_task_is_filed_into_a_project_by_its_ext_id(client, agent):
    _create(client)
    r = client.post(
        "/api/agents/eva/tasks/",
        {"ext_id": "T1", "title": "Book the room", "project": "P1"},
        content_type="application/json",
    )

    assert r.status_code == 201, r.content
    assert r.json()["project_ext_id"] == "P1"
    assert r.json()["project_name"] == "UNGA 2026 conference planning"


def test_a_task_without_a_project_is_fine(client, agent):
    """Plenty of work is a one-off. Forcing a project would produce one project
    per task — what the Drive layout warns against."""
    r = client.post("/api/agents/eva/tasks/", {"ext_id": "T1", "title": "Reply to Beth"},
                    content_type="application/json")

    assert r.json()["project_ext_id"] is None


def test_an_unknown_project_reference_keeps_the_task(client, agent):
    """A typo must not cost the agent the work it just recorded; the response
    says plainly that it was filed nowhere."""
    r = client.post("/api/agents/eva/tasks/", {"ext_id": "T1", "title": "x", "project": "P9"},
                    content_type="application/json")

    assert r.status_code == 201
    assert r.json()["project_ext_id"] is None


def test_patching_moves_a_task_between_projects_and_out_again(client, agent):
    _create(client)
    _create(client, name="Other")
    task_id = client.post("/api/agents/eva/tasks/", {"ext_id": "T1", "title": "x",
                                                     "project": "P1"},
                          content_type="application/json").json()["id"]

    moved = client.patch(f"/api/agents/eva/tasks/{task_id}/", {"project": "P2"},
                         content_type="application/json").json()
    assert moved["project_ext_id"] == "P2"

    # Empty string files it out; the task itself survives.
    out = client.patch(f"/api/agents/eva/tasks/{task_id}/", {"project": ""},
                       content_type="application/json").json()
    assert out["project_ext_id"] is None
    assert AgentTask.objects.get(pk=task_id).title == "x"


def test_patching_something_else_leaves_the_project_alone(client, agent):
    """Omitted and empty must differ, or every title edit would unfile a task."""
    _create(client)
    task_id = client.post("/api/agents/eva/tasks/", {"ext_id": "T1", "title": "x",
                                                     "project": "P1"},
                          content_type="application/json").json()["id"]

    patched = client.patch(f"/api/agents/eva/tasks/{task_id}/", {"title": "y"},
                           content_type="application/json").json()

    assert (patched["title"], patched["project_ext_id"]) == ("y", "P1")


def test_the_list_counts_tasks_without_a_query_per_project(client, agent,
                                                           django_assert_num_queries):
    _create(client)
    _create(client, name="Other")
    for i, (project, status) in enumerate(
        [("P1", "in_progress"), ("P1", "done"), ("P2", "suggested")]
    ):
        client.post("/api/agents/eva/tasks/",
                    {"ext_id": f"T{i}", "title": "t", "project": project, "status": status},
                    content_type="application/json")

    listed = {p["ext_id"]: p for p in client.get("/api/agents/eva/projects/").json()}

    assert (listed["P1"]["task_count"], listed["P1"]["open_task_count"]) == (2, 1)
    assert (listed["P2"]["task_count"], listed["P2"]["open_task_count"]) == (1, 1)


def test_closing_a_project_keeps_its_tasks(client, agent):
    """History is the point of a finished project; deleting its tasks with it
    would throw away what the work was."""
    _create(client)
    client.post("/api/agents/eva/tasks/", {"ext_id": "T1", "title": "x", "project": "P1"},
                content_type="application/json")

    done = client.patch("/api/agents/eva/projects/P1/", {"status": "done"},
                        content_type="application/json")

    assert done.json()["status"] == "done"
    assert AgentTask.objects.filter(agent=agent).count() == 1


def test_a_project_can_be_fetched_by_ext_id_or_numeric_id(client, agent):
    created = _create(client).json()

    by_ext = client.get("/api/agents/eva/projects/P1/")
    by_id = client.get(f"/api/agents/eva/projects/{created['id']}/")

    assert by_ext.status_code == by_id.status_code == 200
    assert by_ext.json()["id"] == by_id.json()["id"] == created["id"]


def test_an_unknown_project_is_404_not_a_server_error(client, agent):
    assert client.get("/api/agents/eva/projects/P42/").status_code == 404


def test_reading_needs_membership_and_writing_needs_more(agent):
    """A stranger cannot see the board at all; a member who is not an editor can
    read it but not reshape it — the tiers `_get_agent_or_404` and
    `_agent_for_write` already draw for tasks."""
    stranger = User.objects.create_user("s", "s@dimagi.com", "pw")
    sc = Client()
    sc.force_login(stranger)
    assert sc.get("/api/agents/eva/projects/").status_code == 404

    viewer = User.objects.create_user("v", "v@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=agent.workspace,
                                       role=WorkspaceMembership.VIEWER)
    vc = Client()
    vc.force_login(viewer)
    assert vc.get("/api/agents/eva/projects/").status_code == 200
    assert vc.post("/api/agents/eva/projects/", {"name": "nope"},
                   content_type="application/json").status_code == 403


def test_sync_files_a_task_into_a_project(client, agent):
    """`canopy agent add` upserts through tasks/sync, so a project named there
    has to stick or the CLI could never file anything."""
    _create(client)

    client.post("/api/agents/eva/tasks/sync",
                {"tasks": [{"ext_id": "T1", "title": "Book the room", "project": "P1"}]},
                content_type="application/json")

    assert AgentTask.objects.get(ext_id="T1").project.ext_id == "P1"


def test_a_sync_that_names_no_project_leaves_the_filing_alone(client, agent):
    """The trap: a wholesale sync defaulting `project` to "" would unfile every
    task it touches — so editing a title from the CLI would quietly empty the
    project it belongs to."""
    _create(client)
    client.post("/api/agents/eva/tasks/sync",
                {"tasks": [{"ext_id": "T1", "title": "Book the room", "project": "P1"}]},
                content_type="application/json")

    client.post("/api/agents/eva/tasks/sync",
                {"tasks": [{"ext_id": "T1", "title": "Book the big room"}]},
                content_type="application/json")

    task = AgentTask.objects.get(ext_id="T1")
    assert (task.title, task.project.ext_id) == ("Book the big room", "P1")
