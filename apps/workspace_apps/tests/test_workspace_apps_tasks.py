"""An app page acting on the tasks of ONE declared project, as the viewer (`apps/workspace_apps/tasks.py`).

The template declares `tasks` (which app role reads, which actions exist, which fields each may set);
the project is an app SETTING an admin binds. Within exactly that project, those actions and those
fields, the app role replaces the workspace-role task gates; everything else is a 404. No action ever
starts an agent turn, and history names the person (or "<agent> for <person>")."""
from __future__ import annotations

import copy

import pytest

from apps.agents import services as agent_services
from apps.agents.models import Agent, AgentProject, AgentTask, AgentTaskAction, Label, TaskEvent
from apps.harness.models import Turn
from apps.workspace_apps import manifest as mf
from apps.workspace_apps import services, template_diff
from apps.workspace_apps import tasks as app_tasks
from apps.workspace_apps.models import AppInstallation, AppRoleGrant
from tests.fixtures.workspace_apps import BASE, J, _client, _user

pytestmark = pytest.mark.django_db

CHORES = f"{BASE}/chores"

MANIFEST = {
    "key": "chores", "name": "Chores",
    "roles": {"family": {"capability": "content.write"}, "helper": {"description": "checks chores off"}},
    "settings": {"task_project": {"type": "text", "label": "Chores project", "default": "chores"}},
    "collections": {"note": {"fields": {"text": {"type": "text", "required": True}}}},
    "actions": {"add_note": {"role": "member", "op": "create", "collection": "note", "input": ["text"]}},
    "pages": [{"slug": "week", "code": "function View({app}){return <div>{app.name}</div>}"}],
    "tasks": {
        "project_setting": "task_project", "read": "helper",
        "actions": {
            "add": {"role": "family", "do": "create", "fields": ["title", "notes", "due", "labels", "assignee_email"]},
            "add_rhythm": {"role": "family", "do": "create", "fields": ["title", "recurrence"]},
            "check": {"role": "helper", "do": "done"},
            "uncheck": {"role": "helper", "do": "reopen"},
            "note": {"role": "helper", "do": "comment"},
            "this_week": {"role": "family", "do": "spawn"},
            "edit": {"role": "helper", "do": "patch", "fields": ["title", "due", "priority"]},
        },
    },
}


@pytest.fixture()
def chores(world):
    ws = world["ws"]
    bailey = Agent.objects.create(slug="bailey", name="Bailey", workspace=ws, owner=world["admin"])
    project = AgentProject.objects.create(workspace=ws, handle="chores", name="Chores", owner_user=world["admin"])
    other = AgentProject.objects.create(workspace=ws, handle="work", name="Work", owner_user=world["admin"])
    secret = AgentProject.objects.create(workspace=ws, handle="secret", name="Secret", owner_user=world["admin"],
                                         visibility=AgentProject.PERSONAL)
    r = _client(world["admin"]).post(f"{BASE}/", {"manifest": MANIFEST}, content_type=J)
    assert r.status_code == 201, r.content
    inst = AppInstallation.objects.get(workspace_id="dimagi", key="chores")
    # The viewer helps (by name); the editor is family (content.write) and helps too.
    for u in (world["viewer"], world["editor"]):
        AppRoleGrant.objects.create(installation=inst, user=u, role="helper", granted_by=world["admin"])
    t = {}
    for name, proj, agent in (("trash", project, bailey), ("dishes", project, None), ("report", other, None),
                              ("diary", secret, None)):
        [t[name]] = agent_services.create_tasks(None, [{"title": name, "project": proj.address, "status": "todo",
                                                        "assignee": agent.slug if agent else ""}],
                                                workspace="dimagi", by_user=world["admin"])
    return dict(world, inst=inst, bailey=bailey, project=project, other=other, secret=secret, t=t)


def _do(client, name, key=None, **input):
    body = {"input": input} | ({"key": key} if key else {})
    return client.post(f"{CHORES}/tasks/actions/{name}", body, content_type=J)


def _tasks(client, **q):
    return client.get(f"{CHORES}/tasks", q)


# --- the declaration ------------------------------------------------------------------------


def _bad(mutate):
    m = copy.deepcopy(MANIFEST)
    mutate(m["tasks"])
    return m


@pytest.mark.parametrize("mutate,needle", [
    (lambda t: t["actions"]["edit"]["fields"].append("status"), "fields"),
    (lambda t: t["actions"]["edit"]["fields"].append("assignee_user"), "fields"),
    (lambda t: t["actions"]["edit"]["fields"].append("project"), "fields"),
    (lambda t: t["actions"]["add"]["fields"].append("agent"), "fields"),
    (lambda t: t["actions"]["add"]["fields"].append("on_approve"), "fields"),
    (lambda t: t["actions"]["edit"]["fields"].append("recurrence"), "fields"),
    (lambda t: t["actions"]["check"].update(fields=["title"]), "fields"),
    (lambda t: t["actions"]["edit"].update(fields=[]), "fields"),
    (lambda t: t["actions"]["add"].update(fields=["notes"]), "title"),
    (lambda t: t["actions"]["check"].update(do="approve"), "do"),
    (lambda t: t["actions"]["check"].update(role="public"), "role"),
    (lambda t: t["actions"]["check"].update(role="nobody"), "role"),
    (lambda t: t["actions"]["check"].update(on_approve=[{}]), "takes"),
    (lambda t: t.update(read="link"), "read"),
    (lambda t: t.update(project_setting="nope"), "project_setting"),
    (lambda t: t.update(project="dimagi/chores"), "project"),
    (lambda t: t.update(actions={}), "actions"),
])
def test_a_bad_tasks_declaration_is_refused_naming_what_is_wrong(mutate, needle):
    with pytest.raises(ValueError, match=needle):
        mf.validate(_bad(mutate))


def test_the_project_setting_must_be_text():
    m = copy.deepcopy(MANIFEST)
    m["settings"]["task_project"]["type"] = "bool"
    m["settings"]["task_project"]["default"] = False
    with pytest.raises(ValueError, match="project_setting"):
        mf.validate(m)


def test_a_workspace_cannot_add_or_widen_tasks_through_its_overlay(chores):
    admin = _client(chores["admin"])
    assert admin.post(f"{CHORES}/draft", {}, content_type=J).status_code == 201
    widen = {"op": "set_setting", "key": "task_project", "value": "chores",
             "tasks": {"read": "member"}}
    r = admin.post(f"{CHORES}/draft/ops", widen, content_type=J)
    assert r.status_code == 422 and "tasks" in r.json()["detail"]
    # An op slipping a change to `tasks` past `_apply` is quarantined by compose, never run.
    m, q = services.ov.compose(chores["inst"].live.template_version.manifest,
                               [{"op": "add_role", "id": "x1", "key": "x_sneaky", "tasks": {"read": "member"}}])
    assert q and m["tasks"]["read"] == "helper"


def test_rebinding_the_project_is_an_admins_change(chores):
    inst = chores["inst"]
    AppRoleGrant.objects.create(installation=inst, user=chores["editor"], role="maintainer", granted_by=chores["admin"])
    editor = _client(chores["editor"])
    assert editor.post(f"{CHORES}/draft", {}, content_type=J).status_code == 201
    r = editor.post(f"{CHORES}/draft/ops", {"op": "set_setting", "key": "task_project", "value": "work"},
                    content_type=J)
    assert r.status_code == 201, r.content
    risks = r.json()["review"]["risks"]
    assert risks and "project" in risks[0]["risk"]
    r = editor.post(f"{CHORES}/draft/publish")
    assert r.status_code == 403 and "admin" in r.json()["detail"]
    assert _client(chores["admin"]).post(f"{CHORES}/draft/publish").status_code == 200
    inst.refresh_from_db()
    assert app_tasks.project_of(inst) == chores["other"]


def test_a_template_update_touching_tasks_needs_an_admin():
    old = mf.validate(copy.deepcopy(MANIFEST))
    wider = copy.deepcopy(MANIFEST)
    wider["tasks"]["read"] = "member"
    assert any("task" in f for f in template_diff.changes(old, mf.validate(wider)))
    moved = copy.deepcopy(MANIFEST)
    moved["settings"]["task_project"]["default"] = "work"
    assert any("project" in f for f in template_diff.changes(old, mf.validate(moved)))
    assert template_diff.changes(old, mf.validate(copy.deepcopy(MANIFEST))) == []


def test_scenarios_cannot_run_task_actions():
    m = copy.deepcopy(MANIFEST)
    m["scenarios"] = [{"name": "check one", "steps": [{"act": "check", "input": {}}]}]
    with pytest.raises(ValueError, match="action of this app"):
        mf.validate(m)


# --- who may ----------------------------------------------------------------------------------


def test_reading_needs_the_declared_role(chores):
    assert _tasks(_client(chores["outsider"])).status_code == 404
    AppRoleGrant.objects.filter(user=chores["editor"], role="helper").delete()
    assert _tasks(_client(chores["editor"])).status_code == 403
    r = _tasks(_client(chores["viewer"]))
    assert r.status_code == 200
    assert sorted(x["title"] for x in r.json()) == ["dishes", "trash"]


def test_the_app_role_replaces_the_workspace_task_gates_for_its_actions_only(chores):
    viewer, key = _client(chores["viewer"]), chores["t"]["dishes"].key
    # A viewer may not change a task on the board...
    assert viewer.patch(f"/api/tasks/{key}/", {"title": "x"}, content_type=J).status_code == 403
    # ...but may through the app's declared action, as the helper it is.
    assert _do(viewer, "edit", key, title="Wash the dishes").status_code == 200
    assert AgentTask.objects.get(key=key).title == "Wash the dishes"
    # Not the family's actions, and nothing the app did not declare.
    assert _do(viewer, "add", title="Mop").status_code == 403
    assert _do(viewer, "delete", key).status_code == 404
    assert _do(_client(chores["outsider"]), "check", key).status_code == 404


def test_a_switched_off_app_acts_on_nothing(chores):
    chores["inst"].enabled = False
    chores["inst"].save()
    assert _do(_client(chores["viewer"]), "check", chores["t"]["dishes"].key).status_code == 404


# --- the one project ----------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["report", "diary"])
def test_a_task_outside_the_declared_project_is_not_there(chores, which):
    # Even the personal project's own lead, holding every app role: it is not the app's project.
    AppRoleGrant.objects.create(installation=chores["inst"], user=chores["admin"], role="helper",
                                granted_by=chores["admin"])
    for who in ("viewer", "admin"):
        c = _client(chores[who])
        key = chores["t"][which].key
        assert _do(c, "check", key).status_code == 404
        assert _do(c, "note", key, body="hi").status_code == 404
        assert key not in [x["key"] for x in _tasks(c).json()]
    assert AgentTask.objects.get(key=chores["t"][which].key).status == AgentTask.TODO


def test_a_personal_project_stays_personal_even_when_declared(chores):
    inst = chores["inst"]
    services.add_op(services.start_draft(inst, user=chores["admin"]),
                    {"op": "set_setting", "key": "task_project", "value": "secret"}, by="admin")
    services.publish(services.open_draft(inst), user=chores["admin"], may_approve_risk=True)
    viewer = _client(chores["viewer"])
    assert _tasks(viewer).status_code == 404
    assert _do(viewer, "check", chores["t"]["diary"].key).status_code == 404
    # Its lead still reads it through the app.
    AppRoleGrant.objects.create(installation=inst, user=chores["admin"], role="helper", granted_by=chores["admin"])
    assert [x["title"] for x in _tasks(_client(chores["admin"])).json()] == ["diary"]


def test_an_unbound_project_is_a_404(chores):
    chores["project"].delete()
    assert _tasks(_client(chores["viewer"])).status_code == 404


def test_filters(chores):
    viewer = _client(chores["viewer"])
    AgentTask.objects.filter(pk=chores["t"]["trash"].pk).update(due="2026-11-10")
    assert [x["title"] for x in _tasks(viewer, due_after="2026-11-01").json()] == ["trash"]
    assert [x["title"] for x in _tasks(viewer, q="dish").json()] == ["dishes"]
    assert _tasks(viewer, status="done").json() == []


# --- the fields ------------------------------------------------------------------------------


@pytest.mark.parametrize("name,input", [
    ("edit", {"status": "done"}),
    ("edit", {"notes": "not declared for edit"}),
    ("edit", {"assignee_user": "viewer@dimagi.com"}),
    ("edit", {"project": "dimagi/work"}),
    ("check", {"title": "x"}),
])
def test_only_declared_fields_are_taken(chores, name, input):
    task = chores["t"]["dishes"]
    r = _do(_client(chores["editor"]), name, task.key, **input)
    assert r.status_code == 422, r.content
    task.refresh_from_db()
    assert (task.title, task.status, task.project_id) == ("dishes", AgentTask.TODO, chores["project"].pk)


def test_create_files_into_the_project_with_no_agent(chores):
    Label.objects.create(workspace_id="dimagi", name="weekly")
    editor = _client(chores["editor"])
    r = _do(editor, "add", title="Mop the floor", due="2026-11-14", labels=["weekly"],
            assignee_email="viewer@dimagi.com")
    assert r.status_code == 201, r.content
    task = AgentTask.objects.get(key=r.json()["key"])
    assert task.project == chores["project"] and task.agent is None and task.created_by_agent is None
    assert task.assignee_user == chores["viewer"] and task.created_by_user == chores["editor"]
    assert task.status == AgentTask.TODO
    # An app never makes labels, never gives work to someone outside the app, nor to an agent.
    assert _do(editor, "add", title="x", labels=["brand-new"]).status_code == 422
    assert not Label.objects.filter(name="brand-new").exists()
    assert _do(editor, "add", title="x", assignee_email="outsider@dimagi.com").status_code == 422
    assert _do(editor, "add", title="x", assignee_email="bailey").status_code == 422


def test_create_a_recurring_template_when_declared(chores):
    r = _do(_client(chores["editor"]), "add_rhythm", title="Trash", recurrence={"every": "week", "weekday": "thu"})
    assert r.status_code == 201, r.content
    assert r.json()["recurrence"]["every"] == "week" and r.json()["status"] == AgentTask.BACKLOG


# --- no turn, ever; attribution ---------------------------------------------------------------


def test_no_task_action_starts_a_turn_or_queues_the_agent(chores):
    trash = chores["t"]["trash"]  # assigned to Bailey
    editor = _client(chores["editor"])
    template = _do(editor, "add_rhythm", title="Laundry", recurrence={"every": "week"}).json()["key"]
    assert _do(editor, "edit", trash.key, title="Bins out").status_code == 200
    assert _do(editor, "note", trash.key, body="the car wash was closed").status_code == 200
    assert _do(editor, "check", trash.key).status_code == 200
    assert _do(editor, "uncheck", trash.key).status_code == 200
    assert _do(editor, "this_week", template, due="2026-11-12").status_code == 201
    assert Turn.objects.count() == 0
    assert not AgentTaskAction.objects.filter(status=AgentTaskAction.PENDING).exists()
    trash.refresh_from_db()
    assert trash.status == AgentTask.TODO and trash.agent == chores["bailey"]


def test_reopen_only_reopens_a_finished_task(chores):
    viewer, key = _client(chores["viewer"]), chores["t"]["dishes"].key
    assert _do(viewer, "uncheck", key).status_code == 409
    assert _do(viewer, "check", key).json()["status"] == AgentTask.DONE
    assert _do(viewer, "uncheck", key).json()["status"] == AgentTask.TODO


def test_history_names_the_person(chores):
    key = chores["t"]["dishes"].key
    _do(_client(chores["viewer"]), "check", key)
    events = TaskEvent.objects.filter(task__key=key).exclude(kind=TaskEvent.CREATED)  # the fixture made it
    assert events.exists() and all(e.by_user == chores["viewer"] and e.by_agent is None for e in events)


def test_an_agent_acting_for_a_verified_asker_is_recorded_as_both(chores):
    inst, key, bailey, viewer = chores["inst"], chores["t"]["dishes"].key, chores["bailey"], chores["viewer"]
    app_tasks.act_task(inst, viewer, "note", key, {"body": "done by email"}, on_behalf_by_agent=bailey)
    app_tasks.act_task(inst, viewer, "check", key, {}, on_behalf_by_agent=bailey)
    events = TaskEvent.objects.filter(task__key=key).exclude(kind=TaskEvent.CREATED)
    assert {e.kind for e in events} >= {TaskEvent.COMMENT, TaskEvent.ACTION, TaskEvent.FIELD}
    assert all(e.by_user == viewer and e.by_agent == bailey for e in events)
    assert all(a.by == "bailey for viewer@dimagi.com" for a in AgentTaskAction.objects.filter(task__key=key))
    # The asker's own app role decides: an agent cannot lend them one they lack.
    with pytest.raises(services.Refused):
        app_tasks.act_task(inst, viewer, "add", None, {"title": "x"}, on_behalf_by_agent=bailey)
    assert Turn.objects.count() == 0


# --- drafts -------------------------------------------------------------------------------------


def test_a_drafts_test_copy_never_touches_live_tasks(chores):
    admin = _client(chores["admin"])
    draft = admin.post(f"{CHORES}/draft", {}, content_type=J).json()["id"]
    key = chores["t"]["dishes"].key
    assert admin.get(f"{CHORES}/tasks?draft={draft}").status_code == 409
    r = admin.post(f"{CHORES}/tasks/actions/check?draft={draft}", {"key": key, "input": {}}, content_type=J)
    assert r.status_code == 409
    assert AgentTask.objects.get(key=key).status == AgentTask.TODO


def test_the_page_is_told_what_is_declared(chores):
    out = _client(chores["viewer"]).get(CHORES).json()
    declared = out["data_model"]["_tasks"]
    assert declared["read"] == "helper" and declared["actions"]["edit"] == {
        "role": "helper", "do": "patch", "fields": ["title", "due", "priority"], "label": "edit"}
    # An app with no declaration says so.
    m = copy.deepcopy(MANIFEST)
    m.pop("tasks")
    m["key"] = "plain"
    assert _client(chores["admin"]).post(f"{BASE}/", {"manifest": m}, content_type=J).status_code == 201
    assert _client(chores["viewer"]).get(f"{BASE}/plain").json()["data_model"]["_tasks"] is None
    assert _tasks(_client(chores["viewer"])).status_code == 200
    assert _client(chores["viewer"]).get(f"{BASE}/plain/tasks").status_code == 404


def test_a_user_with_no_login_reaches_nothing():
    user = _user("ghost")
    assert _tasks(_client(user)).status_code == 404
