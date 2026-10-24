"""Tasks are the workspace's (design 2026-10-24): a key per workspace, an assignee
that is a person or an agent, Linear's status categories, a history of every
change, relations, labels, Triage, saved views, and the agent's old names still
working."""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from apps.agents import work
from apps.agents.models import Agent, AgentProject, AgentTask, TaskEvent, TaskSettings
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
J = "application/json"


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    sam = User.objects.create_user("sam", "sam@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=sam, workspace=ws, role=WorkspaceMembership.VIEWER)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=owner)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner)
    c = Client()
    c.force_login(owner)
    sc = Client()
    sc.force_login(sam)
    return {"owner": owner, "ws": ws, "sam": sam, "eva": eva, "hal": hal, "c": c, "sc": sc}


def _create(c, **body):
    body.setdefault("title", "Draft the deck")
    body.setdefault("workspace", "dimagi")
    r = c.post("/api/tasks/", body, content_type=J)
    assert r.status_code == 201, r.content
    return r.json()


# ---- keys -----------------------------------------------------------------


def test_keys_number_the_workspace_and_never_repeat(w):
    a = _create(w["c"])
    b = _create(w["c"], title="Second")
    other = Workspace.objects.create(slug="connect", display_name="Connect", created_by=w["owner"])
    WorkspaceMembership.objects.create(user=w["owner"], workspace=other, role=WorkspaceMembership.OWNER)
    c = _create(w["c"], workspace="connect")

    assert (a["key"], b["key"], c["key"]) == ("DIM-1", "DIM-2", "CON-1")
    assert a["url"] == "/w/dimagi/tasks/DIM-1" and a["ext_id"] == "DIM-1"
    AgentTask.objects.get(key="DIM-2").delete()
    assert _create(w["c"])["key"] == "DIM-3"


def test_a_prefix_is_derived_and_unique(w):
    for slug in ("dimension", "dim"):
        Workspace.objects.create(slug=slug, display_name=slug, created_by=w["owner"])
    work.settings_for("dimagi")
    assert {work.settings_for(s).prefix for s in ("dimagi", "dimension", "dim")} == {"DIM", "DIME", "DIM2"}


def test_a_prefix_is_frozen_once_a_task_carries_it(w):
    ok = w["c"].patch("/api/task-settings/?workspace=dimagi", {"prefix": "DMG"}, content_type=J)
    assert ok.status_code == 200 and ok.json()["prefix"] == "DMG"
    _create(w["c"])
    frozen = w["c"].patch("/api/task-settings/?workspace=dimagi", {"prefix": "DGI"}, content_type=J)
    assert frozen.status_code == 422


def test_status_names_rename_a_category_and_logic_keeps_the_category(w):
    r = w["c"].patch("/api/task-settings/?workspace=dimagi", {"status_names": {"unstarted": "Ready"}},
                     content_type=J)
    assert r.json()["status_names"]["unstarted"] == "Ready" and r.json()["status_names"]["backlog"] == "Backlog"
    task = _create(w["c"], status="todo")
    detail = w["c"].get(f"/api/tasks/{task['key']}/").json()
    assert (detail["status"], detail["category"], detail["status_names"]["unstarted"]) == ("todo", "unstarted", "Ready")


def test_a_key_outside_your_workspaces_is_not_found(w):
    task = _create(w["c"])
    stranger = User.objects.create_user("x", "x@else.org", "pw")
    sc = Client()
    sc.force_login(stranger)
    assert sc.get(f"/api/tasks/{task['key']}/").status_code == 404
    assert sc.get("/api/agents/eva/tasks/DIM-1/").status_code == 404


# ---- assignees ----------------------------------------------------------------


def test_a_task_is_assigned_to_a_person_or_an_agent_never_both(w):
    by_person = _create(w["c"], assignee="sam@dimagi.com")
    by_agent = _create(w["c"], assignee="agent:hal")
    assert (by_person["assignee_email"], by_person["agent_slug"]) == ("sam@dimagi.com", None)
    assert (by_agent["assignee_email"], by_agent["agent_slug"]) == (None, "hal")

    with pytest.raises(IntegrityError), transaction.atomic():
        AgentTask.objects.filter(key=by_agent["key"]).update(assignee_user=w["sam"])
    moved = w["c"].patch(f"/api/tasks/{by_agent['key']}/", {"assignee": "sam@dimagi.com"}, content_type=J)
    assert (moved.json()["assignee_email"], moved.json()["agent_slug"]) == ("sam@dimagi.com", None)


def test_an_assignee_outside_the_workspace_is_refused(w, ):
    zed_ws = Workspace.objects.create(slug="else", display_name="Else", created_by=w["owner"])
    Agent.objects.create(slug="zed", name="Zed", workspace=zed_ws, owner=w["owner"])
    User.objects.create_user("o", "o@else.org", "pw")
    task = _create(w["c"])
    assert w["c"].patch(f"/api/tasks/{task['key']}/", {"assignee": "o@else.org"},
                        content_type=J).status_code == 422
    assert w["c"].patch(f"/api/tasks/{task['key']}/", {"assignee": "zed"}, content_type=J).status_code == 404


def test_its_assignee_person_may_change_it_and_finish_it(w):
    task = _create(w["c"], assignee="sam@dimagi.com", status="todo")
    other = _create(w["c"])

    mine = w["sc"].patch(f"/api/tasks/{task['key']}/", {"status": "in_progress", "priority": 2}, content_type=J)
    theirs = w["sc"].patch(f"/api/tasks/{other['key']}/", {"title": "x"}, content_type=J)
    done = w["sc"].post(f"/api/tasks/{task['key']}/actions", {"action": "done"}, content_type=J)

    assert mine.status_code == 200 and mine.json()["priority"] == 2
    assert theirs.status_code == 403
    assert done.status_code == 200 and done.json()["task"]["status"] == "done"


def test_giving_a_task_to_an_agent_is_the_editors(w):
    task = _create(w["c"], assignee="sam@dimagi.com")
    r = w["sc"].patch(f"/api/tasks/{task['key']}/", {"assignee": "agent:hal"}, content_type=J)
    assert r.status_code == 403


def test_approving_a_persons_task_starts_it_and_no_turn(w):
    task = _create(w["c"], assignee="sam@dimagi.com", ask_kind="review")
    r = w["sc"].post(f"/api/tasks/{task['key']}/actions", {"action": "approve"}, content_type=J)
    assert r.status_code == 200, r.content
    assert (r.json()["task"]["status"], r.json()["turn_ids"]) == ("in_progress", [])
    assert not Turn.objects.exists()


def test_approving_an_agents_task_starts_its_turn(w):
    task = _create(w["c"], assignee="agent:hal")
    r = w["sc"].post(f"/api/tasks/{task['key']}/actions", {"action": "approve"}, content_type=J)
    assert r.status_code == 200 and len(r.json()["turn_ids"]) == 1
    turn = Turn.objects.get()
    assert turn.agent == w["hal"] and task["key"] in turn.prompt
    detail = w["c"].get(f"/api/tasks/{task['key']}/").json()
    assert detail["turn"]["id"] == str(turn.id) and detail["turn"]["state"]


def test_my_tasks_are_the_ones_assigned_to_me(w):
    _create(w["c"], assignee="sam@dimagi.com", title="Mine")
    _create(w["c"], assignee="agent:hal", title="Hal's")
    mine = w["sc"].get("/api/tasks/?assignee=me").json()
    halls = w["sc"].get("/api/tasks/?assignee=agent:hal").json()
    assert [t["title"] for t in mine] == ["Mine"] and [t["title"] for t in halls] == ["Hal's"]
    assert w["sc"].get("/api/tasks/count/?assignee=me").json() == {"count": 1}


# ---- statuses and Triage ---------------------------------------------------------


def test_accept_and_backlog_decide_without_starting(w):
    one = _create(w["c"], assignee="agent:hal", ask_kind="review")
    two = _create(w["c"], assignee="agent:hal")

    accepted = w["sc"].post(f"/api/tasks/{one['key']}/actions", {"action": "accept"}, content_type=J).json()
    later = w["sc"].post(f"/api/tasks/{two['key']}/actions", {"action": "backlog"}, content_type=J).json()

    assert (accepted["task"]["status"], accepted["task"]["ask_open"], accepted["turn_ids"]) == ("todo", False, [])
    assert (later["task"]["status"], later["turn_ids"]) == ("backlog", [])
    assert not Turn.objects.exists()
    started = w["sc"].post(f"/api/tasks/{two['key']}/actions", {"action": "approve"}, content_type=J).json()
    assert started["task"]["status"] == "in_progress" and len(started["turn_ids"]) == 1


def test_triage_lists_suggestions_ideas_and_proposed_projects_and_snooze_hides_one(w):
    sug = _create(w["c"], title="Suggested")
    _create(w["c"], title="Already on", status="todo")
    AgentProject.objects.create(agent=w["eva"], ext_id="P1", name="New idea", status=AgentProject.PROPOSED)

    before = w["sc"].get("/api/triage/").json()
    snoozed = w["sc"].post(f"/api/tasks/{sug['key']}/actions", {"action": "snooze"}, content_type=J)
    after = w["sc"].get("/api/triage/").json()

    assert sorted((i["kind"], i["title"]) for i in before) == [("project", "New idea"), ("task", "Suggested")]
    assert snoozed.status_code == 200 and snoozed.json()["task"]["snoozed_until"]
    assert [i["title"] for i in after] == ["New idea"]
    # Anything that happens to it wakes it.
    w["c"].patch(f"/api/tasks/{sug['key']}/", {"priority": 1}, content_type=J)
    assert "Suggested" in [i["title"] for i in w["sc"].get("/api/triage/").json()]


def test_a_snooze_until_a_date(w):
    task = _create(w["c"])
    when = (timezone.now() + dt.timedelta(days=3)).date().isoformat()
    r = w["sc"].post(f"/api/tasks/{task['key']}/actions", {"action": "snooze", "comment": when}, content_type=J)
    assert r.json()["task"]["snoozed_until"].startswith(when)
    past = w["sc"].post(f"/api/tasks/{task['key']}/actions", {"action": "snooze", "comment": "2020-01-01"},
                        content_type=J)
    assert past.status_code == 422


def test_duplicate_cancels_it_and_relates_the_two(w):
    first = _create(w["c"], title="Original")
    dup = _create(w["c"], title="Same again")

    r = w["sc"].post(f"/api/tasks/{dup['key']}/actions", {"action": "duplicate", "comment": first["key"]},
                     content_type=J)

    assert r.status_code == 200 and r.json()["task"]["status"] == "declined"
    rel = w["c"].get(f"/api/tasks/{dup['key']}/").json()["relations"]
    assert rel == [{"kind": "duplicate_of", "task": {**rel[0]["task"], "key": first["key"]}}]
    assert w["sc"].post(f"/api/tasks/{first['key']}/actions", {"action": "duplicate", "comment": ""},
                        content_type=J).status_code == 422


def test_the_old_statuses_and_the_new_filter_by_category(w):
    for st in ("suggested", "backlog", "todo", "in_progress", "done", "declined"):
        _create(w["c"], title=st, status=st)
    open_ = w["c"].get("/api/tasks/?category=backlog,unstarted,started").json()
    assert sorted(t["title"] for t in open_) == ["backlog", "in_progress", "todo"]
    sorted_by_status = [t["status"] for t in w["c"].get("/api/tasks/?sort=status").json()]
    assert sorted_by_status == ["suggested", "in_progress", "todo", "backlog", "done", "declined"]


# ---- history ------------------------------------------------------------------


def test_every_change_is_in_its_history_whoever_makes_it(w):
    task = _create(w["c"])
    w["c"].patch(f"/api/tasks/{task['key']}/", {"title": "Draft the talk", "assignee": "agent:hal", "priority": 2,
                                                "labels": ["talk"]}, content_type=J)
    w["sc"].post(f"/api/tasks/{task['key']}/actions", {"action": "reply", "comment": "keep it short"},
                 content_type=J)

    events = w["c"].get(f"/api/tasks/{task['key']}/").json()["events"]
    kinds = [(e["kind"], e["field"]) for e in events]
    assert kinds[0] == ("created", "")
    assert {("field", "title"), ("field", "assignee"), ("field", "priority"), ("field", "labels")} <= set(kinds)
    assignee = next(e for e in events if e["field"] == "assignee")
    assert (assignee["old"], assignee["new"], assignee["by_email"]) == ("", "agent:hal", "jj@dimagi.com")
    reply = next(e for e in events if e["kind"] == "action")
    assert (reply["field"], reply["comment"], reply["by_email"]) == ("reply", "keep it short", "sam@dimagi.com")


def test_an_agents_own_patch_is_recorded_as_the_agent(w):
    bot = User.objects.create_user("hal-bot", "hal@agents.test", "pw")
    WorkspaceMembership.objects.create(user=bot, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    w["hal"].user = bot
    w["hal"].save()
    bc = Client()
    bc.force_login(bot)
    [task] = bc.post("/api/agents/hal/tasks/", [{"title": "Mine"}], content_type=J).json()
    bc.patch(f"/api/agents/hal/tasks/{task['ext_id']}/", {"next_action": "Read the call"}, content_type=J)
    ev = TaskEvent.objects.get(field="next_action")
    assert ev.by_agent == w["hal"]


# ---- relations, labels, bulk --------------------------------------------------


def test_blocks_and_blocked_by_are_one_relation_read_both_ways(w):
    a = _create(w["c"], title="Gather data")
    b = _create(w["c"], title="Write report")

    r = w["c"].post(f"/api/tasks/{b['key']}/relations/", {"kind": "blocked_by", "other": a["key"]}, content_type=J)

    assert r.status_code == 200, r.content
    assert r.json()["blocked"] is True and r.json()["relations"][0]["kind"] == "blocked_by"
    assert w["c"].get(f"/api/tasks/{a['key']}/").json()["relations"][0]["kind"] == "blocks"
    loop = w["c"].post(f"/api/tasks/{a['key']}/relations/", {"kind": "blocked_by", "other": b["key"]},
                       content_type=J)
    assert loop.status_code == 422
    w["c"].post(f"/api/tasks/{a['key']}/actions", {"action": "done"}, content_type=J)
    assert w["c"].get(f"/api/tasks/{b['key']}/").json()["blocked"] is False
    gone = w["c"].delete(f"/api/tasks/{b['key']}/relations/blocked_by/{a['key']}")
    assert gone.status_code == 200 and gone.json()["relations"] == []


def test_labels_are_the_workspaces_and_filter_the_list(w):
    r = w["c"].post("/api/labels/", {"workspace": "dimagi", "name": "Bug", "color": "destructive"}, content_type=J)
    assert r.status_code == 201
    assert w["c"].post("/api/labels/", {"workspace": "dimagi", "name": "bug"}, content_type=J).status_code == 422
    assert w["c"].post("/api/labels/", {"workspace": "dimagi", "name": "x", "color": "red-500"},
                       content_type=J).status_code == 422
    _create(w["c"], title="Broken", labels=["bug"])
    _create(w["c"], title="Fine")
    assert [t["title"] for t in w["c"].get("/api/tasks/?label=Bug").json()] == ["Broken"]
    [label] = w["c"].get("/api/labels/").json()
    assert (label["name"], label["task_count"]) == ("Bug", 1)
    assert w["sc"].post("/api/labels/", {"workspace": "dimagi", "name": "Nope"}, content_type=J).status_code == 403


def test_bulk_changes_each_task_under_its_own_gate(w):
    mine = _create(w["c"], assignee="sam@dimagi.com")
    not_mine = _create(w["c"])

    r = w["sc"].post("/api/tasks/bulk", {"keys": [mine["key"], not_mine["key"]], "patch": {"priority": 1}},
                     content_type=J)

    assert [(x["key"], x["ok"]) for x in r.json()] == [(mine["key"], True), (not_mine["key"], False)]
    assert AgentTask.objects.get(key=not_mine["key"]).priority == 0
    decided = w["sc"].post("/api/tasks/bulk", {"keys": [not_mine["key"]], "action": {"action": "backlog"}},
                           content_type=J).json()
    assert decided[0]["ok"] and decided[0]["task"]["status"] == "backlog"


def test_a_drag_places_a_task_between_two(w):
    a, b, c = (_create(w["c"], title=t) for t in "abc")
    # A new task goes on top: the list reads c, b, a.
    assert [t["title"] for t in w["c"].get("/api/tasks/?sort=rank").json()] == ["c", "b", "a"]
    w["c"].patch(f"/api/tasks/{a['key']}/", {"before": c["key"], "after": b["key"]}, content_type=J)
    assert [t["title"] for t in w["c"].get("/api/tasks/?sort=rank").json()] == ["c", "a", "b"]


# ---- saved views -----------------------------------------------------------


def test_a_view_is_yours_until_an_editor_shares_it(w):
    mine = w["sc"].post("/api/views/", {"workspace": "dimagi", "name": "My bugs",
                                        "filters": {"assignee": "me", "label": "bug"}}, content_type=J)
    shared = w["sc"].post("/api/views/", {"workspace": "dimagi", "name": "Team", "shared": True}, content_type=J)
    team = w["c"].post("/api/views/", {"workspace": "dimagi", "name": "Team", "shared": True, "layout": "board"},
                       content_type=J)
    bad = w["c"].post("/api/views/", {"workspace": "dimagi", "name": "x", "filters": {"nope": "1"}}, content_type=J)

    assert mine.status_code == 201 and shared.status_code == 403 and team.status_code == 201
    assert bad.status_code == 422
    sams = {v["name"]: v["mine"] for v in w["sc"].get("/api/views/").json()}
    jjs = {v["name"] for v in w["c"].get("/api/views/").json()}
    assert sams == {"My bugs": True, "Team": False} and jjs == {"Team"}
    assert w["sc"].delete(f"/api/views/{team.json()['id']}").status_code == 404


# ---- the agent's old names ----------------------------------------------------


def test_an_agents_alias_still_names_its_task_after_it_is_reassigned(w):
    [task] = w["c"].post("/api/agents/eva/tasks/", [{"title": "Eva's"}], content_type=J).json()
    assert (task["ext_id"], task["key"], task["agent_slug"]) == ("T1", "DIM-1", "eva")

    w["c"].patch("/api/tasks/DIM-1/", {"assignee": "agent:hal"}, content_type=J)

    assert w["c"].get("/api/agents/eva/tasks/T1/").json()["agent_slug"] == "hal"
    assert w["c"].get("/api/agents/hal/tasks/DIM-1/").status_code == 200
    assert [t["ext_id"] for t in w["c"].get("/api/agents/hal/tasks/").json()] == ["T1"]
    assert w["c"].get("/api/agents/eva/tasks/").json() == []


def test_an_agents_suggestion_for_a_person_stays_on_its_board(w):
    w["c"].post("/api/agents/eva/tasks/", [{"title": "For Sam", "assignee": "sam@dimagi.com"}], content_type=J)
    [row] = w["c"].get("/api/agents/eva/tasks/").json()
    assert (row["title"], row["assignee_email"], row["agent_slug"]) == ("For Sam", "sam@dimagi.com", None)


def test_moving_a_task_into_another_workspaces_project_gives_it_a_new_key(w):
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=w["owner"],
                                       parent=w["ws"])
    project = AgentProject.objects.create(agent=w["eva"], name="KB", workspace=connect, handle="kb")
    task = _create(w["c"])

    r = w["c"].patch(f"/api/tasks/{task['key']}/", {"project": "connect/kb"}, content_type=J)

    assert r.status_code == 200, r.content
    moved = AgentTask.objects.get(pk=AgentTask.objects.get(project=project).pk)
    assert moved.key.startswith(TaskSettings.objects.get(workspace=connect).prefix)
    assert w["c"].get(f"/api/tasks/{task['key']}/").json()["key"] == moved.key  # the old key still finds it


# ---- projects: milestones, updates, health ----------------------------------------


@pytest.fixture()
def project(w):
    return AgentProject.objects.create(agent=w["eva"], ext_id="P1", name="UNGA", handle="unga")


def test_milestones_count_their_tasks(w, project):
    r = w["c"].post("/api/workspace-projects/dimagi/unga/milestones/", {"name": "Agenda"}, content_type=J)
    assert r.status_code == 201
    a = _create(w["c"], project="dimagi/unga", milestone="Agenda")
    _create(w["c"], project="dimagi/unga", milestone="Agenda")
    w["c"].post(f"/api/tasks/{a['key']}/actions", {"action": "done"}, content_type=J)

    [m] = w["c"].get("/api/workspace-projects/dimagi/unga/milestones/").json()
    detail = w["c"].get("/api/workspace-projects/dimagi/unga/").json()

    assert (m["name"], m["done"], m["total"]) == ("Agenda", 1, 2)
    assert detail["progress"]["total"] == 2 and detail["progress"]["done"] == 1
    assert detail["milestones"][0]["done"] == 1
    assert w["c"].post("/api/workspace-projects/dimagi/unga/milestones/", {"name": "Agenda"},
                       content_type=J).status_code == 422


def test_an_agent_drafts_an_update_and_a_person_publishes_it(w, project):
    bot = User.objects.create_user("eva-bot", "eva@agents.test", "pw")
    WorkspaceMembership.objects.create(user=bot, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    w["eva"].user = bot
    w["eva"].save()
    bc = Client()
    bc.force_login(bot)

    draft = bc.post("/api/workspace-projects/dimagi/unga/updates/", {"health": "on_track", "body": "Agenda set"},
                    content_type=J).json()
    assert (draft["status"], draft["author_agent"]) == ("draft", "eva")
    assert bc.post(f"/api/workspace-projects/dimagi/unga/updates/{draft['id']}/publish", {},
                   content_type=J).status_code == 403
    assert w["sc"].get("/api/workspace-projects/dimagi/unga/updates/").json() == []

    pub = w["c"].post(f"/api/workspace-projects/dimagi/unga/updates/{draft['id']}/publish", {"health": "at_risk"},
                      content_type=J).json()

    assert (pub["status"], pub["health"], pub["published_by_email"]) == ("published", "at_risk", "jj@dimagi.com")
    row = w["c"].get("/api/projects/?status=active").json()[0]
    assert (row["health"], row["update_missing"]) == ("at_risk", False)


def test_an_active_project_with_no_recent_update_shows_it_missing(w, project):
    AgentProject.objects.filter(pk=project.pk).update(created_at=timezone.now() - dt.timedelta(days=10))
    assert w["c"].get("/api/projects/").json()[0]["update_missing"] is True
    w["c"].post("/api/workspace-projects/dimagi/unga/updates/", {"health": "on_track"}, content_type=J)
    assert w["c"].get("/api/projects/").json()[0]["update_missing"] is False


def test_teammates_see_a_projects_work_and_progress(w, project):
    _create(w["c"], project="dimagi/unga")
    detail = w["sc"].get("/api/workspace-projects/dimagi/unga/").json()
    assert len(detail["tasks"]) == 1 and detail["progress"]["total"] == 1 and detail["may_edit"] is False


def test_a_project_spans_agents(w, project):
    r = w["c"].patch("/api/workspace-projects/dimagi/unga/", {"member_agents": ["hal"]}, content_type=J)
    assert r.json()["member_agents"] == ["eva", "hal"]
    assert [p["handle"] for p in w["c"].get("/api/agents/hal/projects/").json()] == ["unga"]
    _create(w["c"], project="dimagi/unga", assignee="agent:hal")
    _create(w["c"], project="dimagi/unga", assignee="agent:eva")
    assert {t["agent_slug"] for t in w["c"].get("/api/workspace-projects/dimagi/unga/").json()["tasks"]} == {"eva",
                                                                                                         "hal"}
