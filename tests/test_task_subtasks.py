"""Tasks get subtasks and a Drive folder, instead of subprojects (T72).

Jonathan asked (2026-10-09) whether a big piece of work inside a product area
should be a "sub project or tasks (and eventually subtasks)". Tasks plus
subtasks: one board per product area. A subtask lives in its parent's project,
under the same agent, never in a cycle, and at most three levels deep.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentProject, AgentTask
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

URL = "/api/agents/hal/tasks/"


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def agent(owner):
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    a = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner)
    AgentProject.objects.create(agent=a, ext_id="P5", name="Supply")
    AgentProject.objects.create(agent=a, ext_id="P6", name="Other")
    return a


@pytest.fixture()
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


def _create(client, *tasks):
    return client.post(URL, list(tasks), content_type="application/json")


def _patch(client, ref, **body):
    return client.patch(f"{URL}{ref}/", body, content_type="application/json")


def test_a_subtask_inherits_its_parents_project(client, agent):
    r = _create(client, {"ext_id": "T66", "title": "History", "project": "P5"},
                {"ext_id": "T73", "title": "A step", "parent": "T66"})

    assert r.status_code == 201, r.content
    child = r.json()[1]
    assert child["parent_ext_id"] == "T66"
    assert child["project_ext_id"] == "P5"
    assert r.json()[0]["parent_ext_id"] is None


def test_a_subtask_in_another_project_is_refused(client, agent):
    _create(client, {"ext_id": "T66", "title": "History", "project": "P5"})

    r = _create(client, {"title": "Elsewhere", "project": "P6", "parent": "T66"})

    assert r.status_code == 422
    assert "parent's project" in r.json()["detail"]
    assert not AgentTask.objects.filter(title="Elsewhere").exists()


def test_another_agents_task_cannot_be_a_parent(client, agent, owner):
    other = Agent.objects.create(slug="eva", name="Eva", workspace=agent.workspace, owner=owner)
    AgentTask.objects.create(agent=other, ext_id="T900", title="Eva's")

    r = _create(client, {"title": "Mine", "parent": "T900"})

    assert r.status_code == 422
    assert "not a task of hal" in r.json()["detail"]


def test_a_cycle_is_refused(client, agent):
    _create(client, {"ext_id": "T1", "title": "a", "project": "P5"},
            {"ext_id": "T2", "title": "b", "parent": "T1"})

    for target, parent in (("T1", "T2"), ("T1", "T1")):
        r = _patch(client, target, parent=parent)
        assert r.status_code == 422, r.content
        assert "own ancestor" in r.json()["detail"]
    assert AgentTask.objects.get(ext_id="T1").parent_id is None


def test_depth_is_capped_at_three_levels(client, agent):
    _create(client, {"ext_id": "T1", "title": "a"}, {"ext_id": "T2", "title": "b", "parent": "T1"},
            {"ext_id": "T3", "title": "c", "parent": "T2"})

    too_deep = _create(client, {"ext_id": "T4", "title": "d", "parent": "T3"})
    assert too_deep.status_code == 422
    assert "at most 3" in too_deep.json()["detail"]

    # Re-parenting a task that has its own subtasks counts the whole subtree.
    _create(client, {"ext_id": "T5", "title": "e"}, {"ext_id": "T6", "title": "f", "parent": "T5"})
    r = _patch(client, "T5", parent="T2")
    assert r.status_code == 422
    assert _patch(client, "T5", parent="T1").status_code == 200


def test_patch_sets_and_clears_the_parent(client, agent):
    _create(client, {"ext_id": "T1", "title": "a", "project": "P5"},
            {"ext_id": "T2", "title": "b", "project": "P5"})

    assert _patch(client, "T2", parent="T1").json()["parent_ext_id"] == "T1"
    # A patch that does not name `parent` leaves it alone.
    assert _patch(client, "T2", title="b2").json()["parent_ext_id"] == "T1"
    assert _patch(client, "T2", parent="").json()["parent_ext_id"] is None


def test_moving_a_parent_moves_its_subtasks(client, agent):
    _create(client, {"ext_id": "T1", "title": "a", "project": "P5"},
            {"ext_id": "T2", "title": "b", "parent": "T1"},
            {"ext_id": "T3", "title": "c", "parent": "T2"})

    r = _patch(client, "T1", project="P6")

    assert r.status_code == 200
    assert {t.ext_id: t.project.ext_id for t in AgentTask.objects.all()} == {
        "T1": "P6", "T2": "P6", "T3": "P6"}


def test_moving_only_a_subtask_out_of_its_parents_project_is_refused(client, agent):
    _create(client, {"ext_id": "T1", "title": "a", "project": "P5"},
            {"ext_id": "T2", "title": "b", "parent": "T1"})

    assert _patch(client, "T2", project="P6").status_code == 422
    # Detaching and moving in one patch is fine.
    r = _patch(client, "T2", project="P6", parent="")
    assert r.status_code == 200
    assert (r.json()["project_ext_id"], r.json()["parent_ext_id"]) == ("P6", None)


def test_deleting_a_parent_promotes_its_subtasks(client, agent):
    _create(client, {"ext_id": "T1", "title": "a"}, {"ext_id": "T2", "title": "b", "parent": "T1"})

    AgentTask.objects.get(ext_id="T1").delete()

    assert AgentTask.objects.get(ext_id="T2").parent_id is None


def test_list_filters_by_parent(client, agent):
    _create(client, {"ext_id": "T1", "title": "a"}, {"ext_id": "T2", "title": "b", "parent": "T1"},
            {"ext_id": "T3", "title": "c", "parent": "T1"}, {"ext_id": "T4", "title": "d"})

    kids = client.get(URL, {"parent": "t1"}).json()
    tops = client.get(URL, {"parent": "none"}).json()

    assert sorted(t["ext_id"] for t in kids) == ["T2", "T3"]
    assert sorted(t["ext_id"] for t in tops) == ["T1", "T4"]
    fleet = client.get("/api/tasks/", {"agent": "hal", "parent": "T1"})
    assert sorted(t["ext_id"] for t in fleet.json()) == ["T2", "T3"]


def test_the_drive_folder_is_carried_on_create_and_patch(client, agent):
    r = _create(client, {"ext_id": "T71", "title": "RUTF tender", "project": "P5",
                         "drive_folder_id": "1qSM", "drive_folder_url": "https://drive.google.com/a"})
    assert (r.json()[0]["drive_folder_id"], r.json()[0]["drive_folder_url"]) == (
        "1qSM", "https://drive.google.com/a")

    p = _patch(client, "T71", drive_folder_id="2X", drive_folder_url="https://drive.google.com/b")

    assert (p.json()["drive_folder_id"], p.json()["drive_folder_url"]) == (
        "2X", "https://drive.google.com/b")
    detail = client.get(f"{URL}T71/").json()
    assert detail["drive_folder_id"] == "2X"
