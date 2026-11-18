"""A project's counts are over its WORK: a recurring template never completes (each
period's work is its instance), so it is out of scope, done and the list's task figures.
Also: an instance names its template's title, and a project's primary screen is one
canopy page of its own workspace."""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import AgentProject, AgentTask
from apps.agents.services import recurrence
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
J = "application/json"


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="home", display_name="Home", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    Workspace.objects.create(slug="other", display_name="Other", created_by=owner)
    project = AgentProject.objects.create(workspace=ws, handle="chores", name="Chores", owner_user=owner)
    c = Client()
    c.force_login(owner)
    return {"owner": owner, "ws": ws, "project": project, "c": c}


def _templates_and_instances(w, n_templates=3):
    keys = []
    for i in range(n_templates):
        r = w["c"].post("/api/tasks/", {"title": f"Chore {i}", "workspace": "home", "project": "home/chores",
                                        "recurrence": {"every": "week", "weekday": "wed"}}, content_type=J)
        assert r.status_code == 201, r.content
        keys.append(r.json()["key"])
    first = AgentTask.objects.get(key=keys[0])
    a, _ = recurrence.spawn(first, dt.date(2026, 11, 18), by=w["owner"])
    b, _ = recurrence.spawn(first, dt.date(2026, 11, 25), by=w["owner"])
    w["c"].patch(f"/api/tasks/{a.key}/", {"status": "done"}, content_type=J)
    return keys, a, b


def test_progress_leaves_templates_out_so_a_project_can_be_done(w):
    _, a, b = _templates_and_instances(w)
    detail = w["c"].get("/api/workspace-projects/home/chores/").json()
    assert (detail["progress"]["done"], detail["progress"]["total"]) == (1, 2)
    assert (detail["done_task_count"], detail["task_count"], detail["open_task_count"]) == (1, 2, 1)
    assert detail["progress"]["scope_history"][-1]["scope"] == 2
    w["c"].patch(f"/api/tasks/{b.key}/", {"status": "done"}, content_type=J)
    progress = w["c"].get("/api/workspace-projects/home/chores/").json()["progress"]
    assert progress["done"] == progress["total"] == 2


def test_the_project_list_counts_work_only(w):
    _templates_and_instances(w)
    row = w["c"].get("/api/projects/").json()[0]
    assert (row["done_task_count"], row["task_count"], row["open_task_count"]) == (1, 2, 1)


def test_milestone_counts_leave_templates_out(w):
    w["c"].post("/api/workspace-projects/home/chores/milestones/", {"name": "Autumn"}, content_type=J)
    r = w["c"].post("/api/tasks/", {"title": "Leaves", "workspace": "home", "project": "home/chores",
                                    "milestone": "Autumn", "recurrence": {"every": "month"}}, content_type=J)
    assert r.status_code == 201, r.content
    plain = w["c"].post("/api/tasks/", {"title": "Gutters", "workspace": "home", "project": "home/chores",
                                        "milestone": "Autumn"}, content_type=J)
    assert plain.status_code == 201, plain.content
    [m] = w["c"].get("/api/workspace-projects/home/chores/").json()["milestones"]
    assert (m["done"], m["total"]) == (0, 1)


def test_an_instance_names_its_template_title(w):
    keys, a, _ = _templates_and_instances(w)
    detail = w["c"].get(f"/api/tasks/{a.key}/").json()
    assert (detail["template_key"], detail["template_title"]) == (keys[0], "Chore 0")
    listed = {t["key"]: t for t in w["c"].get("/api/tasks/", {"project": "home/chores"}).json()}
    assert listed[a.key]["template_title"] == "Chore 0"
    assert listed[keys[0]]["template_title"] is None


def _links(w, links):
    return w["c"].patch("/api/workspace-projects/home/chores/", {"links": links}, content_type=J)


def test_a_primary_screen_is_a_page_of_the_projects_workspace(w):
    r = _links(w, [{"label": "Home", "url": "/w/home/apps/home/week", "kind": "primary"},
                   {"label": "Drive", "url": "https://drive.google.com/x"}])
    assert r.status_code == 200, r.content
    links = w["c"].get("/api/workspace-projects/home/chores/").json()["links"]
    assert {"label": "Home", "url": "/w/home/apps/home/week", "kind": "primary", "waiting_on": ""} in links


@pytest.mark.parametrize("url", [
    "https://evil.example/w/home/x", "//evil.example/w/home", "/w/other/apps/home/week", "/w/home/../other/x",
    "javascript:alert(1)", "/agents/bailey",
])
def test_a_primary_screen_off_this_workspace_is_refused(w, url):
    assert _links(w, [{"label": "Home", "url": url, "kind": "primary"}]).status_code == 422


def test_a_project_has_one_primary_screen_and_a_task_none(w):
    two = [{"label": "A", "url": "/w/home/a", "kind": "primary"}, {"label": "B", "url": "/w/home/b", "kind": "primary"}]
    assert _links(w, two).status_code == 422
    r = w["c"].post("/api/tasks/", {"title": "t", "workspace": "home",
                                    "links": [{"label": "A", "url": "/w/home/a", "kind": "primary"}]}, content_type=J)
    assert r.status_code == 422
