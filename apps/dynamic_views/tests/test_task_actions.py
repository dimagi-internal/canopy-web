"""A project view's `task_actions`: marking its own project's tasks done or reopening them, as the viewer.

Declared on the view (snapshotted per revision), only on a project view, by someone who could move the
project's tasks themselves. At run time `POST /api/dynamic-views/{id}/task-actions` checks the view's
current declaration, that the task is the view's project's (else 404) and that the viewer may edit it
(`may_edit_task`, else 403), then runs the shared writer (`finish_or_reopen`) as the viewer."""
from __future__ import annotations

import pytest
from django.test import Client

from apps.agents import services as agent_services
from apps.agents.models import AgentProject, AgentTask, TaskEvent
from apps.dynamic_views.models import DynamicView
from apps.harness.models import Turn
from apps.workspaces.models import WorkspaceMembership as M
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db

J = "application/json"
CODE = "function View({ canopy }) { return <button onClick={() => canopy.tasks.act('X-1', 'done')}>Done</button>; }"


@pytest.fixture()
def world():
    ws = a_workspace("family")
    owner = a_member(ws, email="jj@dimagi.com", role=M.OWNER)
    editor = a_member(ws, email="ed@dimagi.com", role=M.EDITOR)
    viewer = a_member(ws, email="vi@dimagi.com", role=M.VIEWER)
    outreach = AgentProject.objects.create(workspace=ws, handle="outreach", name="Outreach", owner_user=viewer)
    other = AgentProject.objects.create(workspace=ws, handle="other", name="Other", owner_user=owner)
    t = {}
    for name, proj, assignee in (("mine", outreach, viewer.email), ("theirs", outreach, ""),
                                 ("elsewhere", other, viewer.email)):
        [t[name]] = agent_services.create_tasks(
            None, [{"title": name, "project": proj.address, "status": "todo", "assignee": assignee}],
            workspace="family", by_user=owner)
    return dict(ws=ws, owner=owner, editor=editor, viewer=viewer, outreach=outreach, t=t)


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _create(client, **body):
    body = {"workspace": "family", "project": "outreach", "title": "Outreach", "code": CODE,
            "task_actions": ["done", "reopen"], **body}
    return client.post("/api/dynamic-views/", body, content_type=J)


def _view(world, actions=("done", "reopen")) -> int:
    r = _create(_client(world["editor"]), task_actions=list(actions))
    assert r.status_code == 201, r.content
    return r.json()["id"]


def _act(user, view_id, key, action):
    return _client(user).post(f"/api/dynamic-views/{view_id}/task-actions", {"key": key, "action": action},
                              content_type=J)


# --- the declaration -------------------------------------------------------------------------


def test_an_editor_declares_task_actions_on_a_project_view_and_revisions_keep_them(world):
    r = _create(_client(world["editor"]), task_actions=["reopen", "done", "done"])
    assert r.status_code == 201, r.content
    assert r.json()["task_actions"] == ["done", "reopen"]
    view_id = r.json()["id"]
    assert _client(world["viewer"]).get(f"/api/dynamic-views/{view_id}").json()["task_actions"] == ["done", "reopen"]
    c = _client(world["editor"])
    assert c.patch(f"/api/dynamic-views/{view_id}", {"task_actions": ["done"]}, content_type=J).status_code == 200
    revs = c.get(f"/api/dynamic-views/{view_id}/revisions/").json()
    assert [rv["task_actions"] for rv in revs] == [["done"], ["done", "reopen"]]
    # A restore restores the declaration with the code.
    assert c.post(f"/api/dynamic-views/{view_id}/rollback", {"version": 1}, content_type=J).json()["task_actions"] \
        == ["done", "reopen"]


@pytest.mark.parametrize("declared, why", [
    (["done", "delete"], "no action 'delete'"),
    (["approve"], "no action 'approve'"),
    ("done", "valid list"),
    ([{"action": "done"}], "valid string"),
])
def test_an_unknown_action_is_refused_naming_it(world, declared, why):
    r = _create(_client(world["editor"]), task_actions=declared)
    assert r.status_code == 422 and why in r.content.decode(), r.content


def test_a_workspace_view_cannot_declare_task_actions(world):
    r = _create(_client(world["editor"]), project=None)
    assert r.status_code == 422 and "only for a project view" in r.content.decode()


def test_a_view_that_declares_them_cannot_move_to_the_workspace(world):
    view_id = _view(world)
    r = _client(world["editor"]).patch(f"/api/dynamic-views/{view_id}", {"project": ""}, content_type=J)
    assert r.status_code == 422 and "only for a project view" in r.content.decode()
    assert DynamicView.objects.get(pk=view_id).project_id == world["outreach"].pk


def test_the_writer_must_be_able_to_move_the_projects_tasks(world):
    # The project's lead is only a workspace viewer: they edit the project's views, but cannot
    # declare task actions or write new code for a view that has them.
    lead = _client(world["viewer"])
    r = _create(lead)
    assert r.status_code == 422 and "editor role" in r.content.decode()
    assert _create(lead, task_actions=[]).status_code == 201
    view_id = _view(world)
    assert lead.patch(f"/api/dynamic-views/{view_id}", {"title": "Renamed"}, content_type=J).status_code == 200
    r = lead.patch(f"/api/dynamic-views/{view_id}", {"code": CODE + "\n// more"}, content_type=J)
    assert r.status_code == 422 and "editor role" in r.content.decode()


# --- acting ----------------------------------------------------------------------------------


def test_the_viewer_marks_their_task_done_and_reopens_it_and_history_names_them(world):
    view_id = _view(world)
    key = world["t"]["mine"].key
    r = _act(world["viewer"], view_id, key, "done")
    assert r.status_code == 200, r.content
    assert r.json()["key"] == key and r.json()["status"] == AgentTask.DONE
    r = _act(world["viewer"], view_id, key.lower(), "reopen")
    assert r.status_code == 200 and r.json()["status"] == AgentTask.TODO
    events = TaskEvent.objects.filter(task__key=key).exclude(kind=TaskEvent.CREATED)
    assert events.exists() and all(e.by_user == world["viewer"] and e.by_agent is None for e in events)
    assert not Turn.objects.exists()


def test_reopen_only_reopens_a_finished_task(world):
    view_id = _view(world)
    r = _act(world["editor"], view_id, world["t"]["theirs"].key, "reopen")
    assert r.status_code == 409 and "nothing to reopen" in r.content.decode()


def test_an_undeclared_action_is_refused(world):
    view_id = _view(world, actions=("reopen",))
    key = world["t"]["mine"].key
    r = _act(world["editor"], view_id, key, "done")
    assert r.status_code == 404 and "does not declare" in r.content.decode()
    # Nothing outside the shared moves, whatever the view says.
    assert _act(world["editor"], view_id, key, "approve").status_code == 404
    assert AgentTask.objects.get(key=key).status == AgentTask.TODO


def test_a_narrowed_declaration_applies_at_once(world):
    view_id = _view(world)
    _client(world["editor"]).patch(f"/api/dynamic-views/{view_id}", {"task_actions": ["reopen"]}, content_type=J)
    assert _act(world["editor"], view_id, world["t"]["mine"].key, "done").status_code == 404


def test_a_task_of_another_project_is_not_there(world):
    view_id = _view(world)
    key = world["t"]["elsewhere"].key
    for user in (world["viewer"], world["editor"], world["owner"]):
        r = _act(user, view_id, key, "done")
        assert r.status_code == 404 and "in this view's project" in r.content.decode()
    assert _act(world["editor"], view_id, "NOPE-999", "done").status_code == 404
    assert AgentTask.objects.get(key=key).status == AgentTask.TODO


def test_a_viewer_without_edit_rights_on_the_task_is_refused(world):
    view_id = _view(world)
    key = world["t"]["theirs"].key  # in the project, not assigned to the viewer
    r = _act(world["viewer"], view_id, key, "done")
    assert r.status_code == 403
    assert AgentTask.objects.get(key=key).status == AgentTask.TODO
    # An editor may, as on the task's own page.
    assert _act(world["editor"], view_id, key, "done").status_code == 200


def test_a_non_member_reaches_nothing(world):
    view_id = _view(world)
    stranger = a_member(a_workspace("elsewhere"), email="x@else.com", role=M.OWNER)
    assert _act(stranger, view_id, world["t"]["mine"].key, "done").status_code == 404
