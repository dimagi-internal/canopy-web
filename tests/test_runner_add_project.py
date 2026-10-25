"""A person adds a repo to a laptop runner from canopy-web; the laptop makes it an
emdash project on its own (`runner_projects`).

bailey (workspace marshellis) could not run on jj-mbp-cdp although it was enrolled
and first in the order: routing drops a laptop with no emdash project named after
the agent, and the only fix was someone at that Mac cloning the repo and adding it
in emdash by hand. The ask now rides the heartbeat reply, and the project showing
up in the box's own report is what makes routing take it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import routing, runner_projects
from apps.harness.models import Runner, RunnerAdmin, RunnerProjectRequest, WorkspaceRunnerOrder
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.testing import enrolled

pytestmark = pytest.mark.django_db

REPO = "https://github.com/marshellis/bailey"


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


@pytest.fixture
def setup():
    jj = _user("jj")
    ws = Workspace.objects.create(slug="marshellis", display_name="Marshellis", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    box = enrolled(Runner.objects.create(
        name="jj-mbp-cdp", kind=Runner.EMDASH, host="jj@mbp", owner=jj, status=Runner.ONLINE,
        workspace=ws, last_heartbeat_at=timezone.now(), capabilities={"projects": ["ada", "hal"]}))
    bailey = Agent.objects.create(slug="bailey", name="Bailey", workspace=ws, owner=jj, repo_url=REPO)
    c = Client()
    c.force_login(jj)
    return jj, ws, box, bailey, c


def _beat(c, box, **body):
    return c.post(f"/api/harness/runners/{box.id}/heartbeat",
                  data={"active_turn_ids": [], **body}, content_type="application/json")


def _ask(c, box, **body):
    return c.post(f"/api/harness/runners/{box.id}/projects", data=body, content_type="application/json")


def test_an_agents_repo_is_asked_under_its_slug_and_rides_the_heartbeat(setup):
    _jj, _ws, box, _bailey, c = setup
    resp = _ask(c, box, agent="bailey")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert (body["name"], body["repo_url"], body["agent"], body["status"]) == ("bailey", REPO, "bailey", "pending")
    owed = _beat(c, box, projects=["ada", "hal"]).json()["add_projects"]
    assert [(a["name"], a["repo_url"]) for a in owed] == [("bailey", REPO)]


def test_the_box_reporting_the_project_discharges_the_ask_and_routing_takes_it(setup):
    _jj, ws, box, bailey, c = setup
    WorkspaceRunnerOrder.objects.create(workspace=ws, runner=box, rank=0)
    assert routing.inherited_orders([bailey.pk])[bailey.pk].missing_repo == [box]
    _ask(c, box, agent="bailey")
    reply = _beat(c, box, projects=["ada", "bailey", "hal"]).json()
    assert reply["add_projects"] == []
    row = RunnerProjectRequest.objects.get(runner=box, name="bailey")
    assert row.status == RunnerProjectRequest.ADDED and row.finished_at
    inh = routing.inherited_orders([bailey.pk])[bailey.pk]
    assert inh.missing_repo == [] and [r.pk for r in inh.runners] == [box.pk]


def test_a_failure_marks_only_that_ask_never_the_box(setup):
    _jj, _ws, box, _bailey, c = setup
    ask_id = _ask(c, box, agent="bailey").json()["id"]
    resp = c.post(f"/api/harness/runners/{box.id}/projects/{ask_id}/result",
                  data={"ok": False, "detail": "git clone: Repository not found"},
                  content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == "failed"
    assert "Repository not found" in resp.json()["detail"]
    box.refresh_from_db()
    assert box.ready is True and box.live_status == Runner.ONLINE
    # No longer owed: a failed ask is not retried until someone asks again.
    assert _beat(c, box, projects=["ada", "hal"]).json()["add_projects"] == []
    # Asking again re-arms the same row.
    again = _ask(c, box, agent="bailey").json()
    assert again["id"] == ask_id and again["status"] == "pending" and again["detail"] == ""


def test_a_reported_success_counts_once_the_project_is_in_the_report(setup):
    _jj, _ws, box, _bailey, c = setup
    ask_id = _ask(c, box, agent="bailey").json()["id"]
    out = c.post(f"/api/harness/runners/{box.id}/projects/{ask_id}/result",
                 data={"ok": True, "detail": "added"}, content_type="application/json").json()
    # The box said so, but its report does not show it yet: still pending...
    assert out["status"] == "pending"
    # ...until the next beat carries it.
    _beat(c, box, projects=["ada", "bailey", "hal"])
    assert RunnerProjectRequest.objects.get(pk=ask_id).status == RunnerProjectRequest.ADDED


def test_a_plain_repo_url_takes_its_own_name(setup):
    _jj, _ws, box, _bailey, c = setup
    body = _ask(c, box, repo_url="git@github.com:dimagi/some-tool.git").json()
    assert body["name"] == "some-tool" and body["agent"] is None


@pytest.mark.parametrize("repo_url,name", [
    ("--upload-pack=touch /tmp/x", ""),
    ("https://github.com/a/b c", ""),
    ("file:///etc", ""),
    (REPO, "../escape"),
    (REPO, "-rf"),
])
def test_unsafe_asks_are_refused(setup, repo_url, name):
    _jj, _ws, box, _bailey, c = setup
    assert _ask(c, box, repo_url=repo_url, name=name).status_code == 422
    assert not RunnerProjectRequest.objects.exists()


def test_an_agent_with_no_repo_is_refused(setup):
    _jj, ws, box, _bailey, c = setup
    Agent.objects.create(slug="norepo", name="No Repo", workspace=ws)
    resp = _ask(c, box, agent="norepo")
    assert resp.status_code == 422 and "no repo" in resp.json()["detail"]


def test_a_cloud_box_is_not_asked(setup):
    jj, ws, _box, _bailey, c = setup
    cloud = enrolled(Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=jj, workspace=ws,
                                           status=Runner.ONLINE, last_heartbeat_at=timezone.now()))
    assert _ask(c, cloud, agent="bailey").status_code == 422
    assert _beat(c, cloud).json()["add_projects"] is None


def test_a_project_already_held_is_recorded_as_added(setup):
    _jj, _ws, box, _bailey, c = setup
    body = _ask(c, box, repo_url="https://github.com/dimagi/ada").json()
    assert body["status"] == "added"
    assert _beat(c, box, projects=["ada", "hal"]).json()["add_projects"] == []


def test_only_its_owner_or_an_administrator_may_ask(setup):
    jj, ws, box, _bailey, _c = setup
    member = _user("m")
    WorkspaceMembership.objects.create(workspace=ws, user=member, role=WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(member)
    assert _ask(c, box, agent="bailey").status_code == 404
    # A member who can see the runner reads the list, but may not ask.
    listing = c.get(f"/api/harness/runners/{box.id}/projects")
    assert listing.status_code == 200 and listing.json()["can_add"] is False
    RunnerAdmin.objects.create(runner=box, user=member, granted_by=jj)
    assert _ask(c, box, agent="bailey").status_code == 201
    assert c.get(f"/api/harness/runners/{box.id}/projects").json()["can_add"] is True


def test_the_list_shows_what_it_holds_and_what_was_asked(setup):
    _jj, _ws, box, _bailey, c = setup
    _ask(c, box, agent="bailey")
    body = c.get(f"/api/harness/runners/{box.id}/projects").json()
    assert body["projects"] == ["ada", "hal"] and body["can_add"] is True
    assert [(r["name"], r["status"], r["requested_by_email"]) for r in body["requests"]] == [
        ("bailey", "pending", "jj@dimagi.com")]


def test_withdrawing_stops_the_ask(setup):
    _jj, _ws, box, _bailey, c = setup
    ask_id = _ask(c, box, agent="bailey").json()["id"]
    assert c.delete(f"/api/harness/runners/{box.id}/projects/{ask_id}").status_code == 204
    assert _beat(c, box, projects=["ada", "hal"]).json()["add_projects"] == []
    # A late report for a withdrawn ask is a 404, not a resurrection.
    late = c.post(f"/api/harness/runners/{box.id}/projects/{ask_id}/result",
                  data={"ok": False, "detail": "x"}, content_type="application/json")
    assert late.status_code == 404


def test_the_default_order_names_the_laptops_by_id_too(setup):
    _jj, ws, box, _bailey, c = setup
    WorkspaceRunnerOrder.objects.create(workspace=ws, runner=box, rank=0)
    body = c.get("/api/w/marshellis/agents/bailey/default-order").json()
    assert body["missing_repo"] == ["jj-mbp-cdp"] and body["missing_repo_ids"] == [str(box.pk)]


def test_name_from_repo_url():
    assert runner_projects.name_from_repo_url(REPO) == "bailey"
    assert runner_projects.name_from_repo_url(REPO + ".git") == "bailey"
    assert runner_projects.name_from_repo_url("git@github.com:dimagi/canopy-web.git") == "canopy-web"

