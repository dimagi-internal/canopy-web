"""A view's `agent_tasks` declaration: the one thing a view may make happen.

Checked when the view is written: each agent must be in the view's workspace and
one its author may give tasks to. At run time the page starts a declared task as
the viewer, so `POST /api/agent-tasks/`'s own gate decides — pinned at the end."""
from __future__ import annotations

import json

import pytest
from django.test import Client

from apps.agents.models import Agent, AgentProject
from apps.dynamic_views.models import DynamicView
from apps.workspaces.models import WorkspaceMembership as M
from apps.workspaces.testing import a_member, a_workspace
from tests.access_helpers import editors_run

pytestmark = pytest.mark.django_db

J = "application/json"
CODE = "function View({ canopy }) { return <button onClick={() => canopy.runAgentTask({})}>Go</button>; }"
REVIEW = {"agent": "bailey", "name": "gmail-triage.review"}
APPLY = {"agent": "bailey", "name": "gmail-triage.apply"}


@pytest.fixture()
def world():
    ws = a_workspace("family")
    owner = a_member(ws, email="jj@dimagi.com", role=M.OWNER)
    editor = a_member(ws, email="ed@dimagi.com", role=M.EDITOR)
    viewer = a_member(ws, email="vi@dimagi.com", role=M.VIEWER)
    bot = a_member(ws, email="bailey@marshellis.com", role=M.VIEWER)
    editors_run(Agent.objects.create(slug="bailey", name="Bailey", workspace=ws, owner=owner, user=bot))
    elsewhere = a_workspace("elsewhere")
    stranger_owner = a_member(elsewhere, email="other@dimagi.com", role=M.OWNER)
    Agent.objects.create(slug="hal", name="Hal", workspace=elsewhere, owner=stranger_owner)
    project = AgentProject.objects.create(workspace=ws, handle="gmail-clean-up", name="Gmail clean-up",
                                          owner_user=viewer)
    return dict(ws=ws, owner=owner, editor=editor, viewer=viewer, project=project)


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _create(client, **body):
    body = {"workspace": "family", "title": "Gmail clean-up", "code": CODE, "agent_tasks": [REVIEW, APPLY],
            **body}
    return client.post("/api/dynamic-views/", body, content_type=J)


def test_an_editor_declares_tasks_and_every_reader_sees_them(world):
    r = _create(_client(world["editor"]))
    assert r.status_code == 201, r.content
    view_id = r.json()["id"]
    assert r.json()["agent_tasks"] == [REVIEW, APPLY]
    # A reader of the view (a workspace viewer) sees what it can make agents do.
    got = _client(world["viewer"]).get(f"/api/dynamic-views/{view_id}").json()
    assert got["agent_tasks"] == [REVIEW, APPLY]
    revs = _client(world["viewer"]).get(f"/api/dynamic-views/{view_id}/revisions/").json()
    assert revs[0]["agent_tasks"] == [REVIEW, APPLY]


def test_a_view_that_declares_nothing_declares_nothing(world):
    r = _create(_client(world["editor"]), agent_tasks=[])
    assert r.status_code == 201 and r.json()["agent_tasks"] == []
    body = {"workspace": "family", "title": "Plain", "code": CODE}
    assert _client(world["editor"]).post("/api/dynamic-views/", body, content_type=J).json()["agent_tasks"] == []


@pytest.mark.parametrize("declared, why", [
    ([{"agent": "nobody", "name": "x"}], "no agent 'nobody' in this view's workspace"),
    # An agent of another workspace is not one of this view's, whoever asks.
    ([{"agent": "hal", "name": "x"}], "no agent 'hal' in this view's workspace"),
    ([{"agent": "bailey"}], "name must be"),
    ([{"agent": "bailey", "name": "../x"}], "name must be"),
    ([{"name": "x"}], "agent is required"),
    ([{"agent": "bailey", "name": "x", "input": {}}], "takes only agent and name"),
    ([REVIEW, REVIEW], "twice"),
    ("bailey", "valid list"),
    ([{"agent": "bailey", "name": f"t{i}"} for i in range(21)], "at most 20"),
])
def test_a_bad_declaration_is_refused_when_the_view_is_written(world, declared, why):
    r = _create(_client(world["editor"]), agent_tasks=declared)
    assert r.status_code == 422 and why in r.content.decode(), r.content


def test_the_author_must_be_able_to_give_the_agent_a_task(world):
    # A project lead who is only a workspace viewer may make that project's views,
    # but not ones that give bailey work: they could not give it a task themselves.
    lead = _client(world["viewer"])
    r = _create(lead, project="gmail-clean-up")
    assert r.status_code == 422 and "needs the editor role" in r.content.decode()
    r = _create(lead, project="gmail-clean-up", agent_tasks=[])
    assert r.status_code == 201
    view_id = r.json()["id"]
    r = lead.patch(f"/api/dynamic-views/{view_id}", {"agent_tasks": [REVIEW]}, content_type=J)
    assert r.status_code == 422 and "needs the editor role" in r.content.decode()


def test_new_code_on_a_view_that_declares_tasks_needs_the_same_right(world):
    view_id = _create(_client(world["editor"]), project="gmail-clean-up").json()["id"]
    lead = _client(world["viewer"])
    # The lead may still retitle it (the frontend sends the declaration back unchanged)...
    r = lead.patch(f"/api/dynamic-views/{view_id}", {"title": "Inbox", "agent_tasks": [REVIEW, APPLY]},
                   content_type=J)
    assert r.status_code == 200 and r.json()["version"] == 2
    # ...but not write new code that calls bailey.
    r = lead.patch(f"/api/dynamic-views/{view_id}", {"code": CODE.replace("Go", "Run")}, content_type=J)
    assert r.status_code == 422 and "needs the editor role" in r.content.decode()
    assert DynamicView.objects.get(pk=view_id).code == CODE


def test_a_change_to_the_declaration_is_a_version_and_rollback_restores_it(world):
    c = _client(world["editor"])
    view_id = _create(c).json()["id"]
    r = c.patch(f"/api/dynamic-views/{view_id}", {"agent_tasks": [REVIEW]}, content_type=J)
    assert (r.json()["version"], r.json()["agent_tasks"]) == (2, [REVIEW])
    r = c.post(f"/api/dynamic-views/{view_id}/rollback", {"version": 1}, content_type=J)
    assert (r.json()["version"], r.json()["agent_tasks"]) == (3, [REVIEW, APPLY])


def test_at_run_time_the_viewer_is_the_one_the_agent_task_gate_judges(world):
    """The page starts a declared task with the VIEWER's session. A workspace
    editor may; a workspace viewer gets the server's refusal (shown to the view)."""
    _create(_client(world["editor"]))
    body = json.dumps({**REVIEW, "input": {"count": 50}, "output_schema": {"type": "object"}})
    assert _client(world["editor"]).post("/api/w/family/agent-tasks/", body, content_type=J).status_code == 201
    r = _client(world["viewer"]).post("/api/w/family/agent-tasks/", body, content_type=J)
    assert r.status_code == 403
