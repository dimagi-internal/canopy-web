"""Ideas are tasks with kind=idea, and a project's team view (hal T82).

Pinned here:

* a task is an idea only when `kind="idea"` — never by inference, however much
  of the agent's own work there is or whose words are on it;
* an idea carries a person's own words: no human-why, no idea, on create and
  on patch alike;
* an idea's stage is its task's status; its subtasks are the work toward it,
  and finishing them does not ship it — marking the idea done does, by its
  owner or its person;
* any member adds their own idea in their own words, and the agent and owner
  are told; an agent cannot use that door;
* the team view's decisions are one line each, from open asks, tasks parked on a
  person and decision links — and every tally on it agrees with every other.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.core import mail

from apps.agents import services as agent_services
from apps.agents.models import AgentProject, AgentTask
from apps.contacts import services as contacts
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.people_brain import _agent, _client, _member

pytestmark = pytest.mark.django_db

BASE = "/api/w/connect/agents/hal/projects/P1"
TASKS = "/api/w/connect/agents/hal/tasks/"


@pytest.fixture()
def world(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan",
                                     last_name="Jackson")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    sophie = _member(ws, "sophie", role=WorkspaceMembership.VIEWER, first_name="Sophie")
    hal = _agent(ws, "hal", owner)
    project = AgentProject.objects.create(agent=hal, ext_id="P1", name="Supply", owner_user=owner,
                                          outcome="One honest record of supply.",
                                          links=[{"label": "Live app", "url": "https://x.test/supply/"}])
    jj = contacts.person_for(user=owner)
    contacts.person_for(user=sophie)
    agent_services.create_tasks(hal, [
        # Process work, even with Jonathan's words on it: never an idea.
        {"ext_id": "T1", "title": "Produce the history docs", "project": "P1", "status": "done",
         "human_why": [{"quote": "craft a short two pager", "who": {"person_id": jj.pk}}]},
        {"ext_id": "T2", "title": "Sophie's tender", "project": "P1", "status": "in_progress"},
        {"ext_id": "T3", "title": "[T2] Screen review", "project": "P1", "parent": "T2", "status": "done"},
    ])
    return {"owner": owner, "ws": ws, "sophie": sophie, "hal": hal, "project": project, "jj": jj}


def _hal(world):
    return _client(world["hal"].user)


def _idea(world, ext_id, quote, status="suggested"):
    [t] = agent_services.create_tasks(world["hal"], [{
        "ext_id": ext_id, "title": quote[:40], "project": "P1", "kind": "idea", "status": status,
        "human_why": [{"quote": quote, "who": {"person_id": world["jj"].pk}}]}])
    return t


def _act(client, ref, action="done"):
    return client.post(f"{TASKS}{ref}/actions", data={"action": action}, content_type="application/json")


def test_work_is_never_an_idea_even_with_a_persons_words_on_it(world):
    team = _client(world["sophie"]).get(f"{BASE}/team/").json()
    assert team["ideas"] == [] and team["built"] == [] and team["in_progress"] == []
    assert team["stages"] == {"proposed": 0, "started": 0, "shipped": 0, "declined": 0}


def test_an_idea_must_carry_a_persons_words(world):
    hal = _hal(world)
    r = hal.post(TASKS, data=[{"title": "Per-org pages", "project": "P1", "kind": "idea"}],
                 content_type="application/json")
    assert r.status_code == 422 and "human_why" in r.json()["detail"]
    # Turning work into an idea needs the words too: T2 has none.
    assert hal.patch(f"{TASKS}T2/", data={"kind": "idea"}, content_type="application/json").status_code == 422
    assert AgentTask.objects.get(ext_id="T2").kind == "work"
    # T1 carries Jonathan's words, so it may become one...
    r = hal.patch(f"{TASKS}T1/", data={"kind": "idea"}, content_type="application/json")
    assert r.status_code == 200 and r.json()["kind"] == "idea"
    # ...and an idea cannot have its words taken away.
    assert hal.patch(f"{TASKS}T1/", data={"human_why": []}, content_type="application/json").status_code == 422
    assert AgentTask.objects.get(ext_id="T1").human_whys.count() == 1


def test_a_member_adds_their_own_idea_and_the_agent_and_owner_are_told(world):
    r = _client(world["sophie"]).post(f"{BASE}/ideas/", data={"quote": "Show me which suppliers never answered"},
                                      content_type="application/json")
    assert r.status_code == 201, r.content
    out = r.json()
    assert out["idea"]["stage"] == "proposed" and out["idea"]["who"]["name"] == "Sophie"
    assert out["idea"]["quote"] == "Show me which suppliers never answered"
    assert out["notified"] == ["Hal", "Jonathan Jackson"]
    task = AgentTask.objects.get(key=out["idea"]["key"])
    assert (task.kind, task.status, task.project_id) == ("idea", "suggested", world["project"].pk)
    turn = Turn.objects.get(idempotency_key=f"idea-{task.pk}")
    assert "never answered" in turn.prompt and turn.initiator_user == world["sophie"]
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["jj@dimagi.com"]


def test_the_agent_cannot_use_the_members_door(world):
    r = _hal(world).post(f"{BASE}/ideas/", data={"quote": "mine"}, content_type="application/json")
    assert r.status_code == 422 and not AgentTask.objects.filter(kind="idea").exists()


def test_subtasks_are_the_work_and_do_not_ship_the_idea(world):
    _idea(world, "T10", "something sophie can start using", status="in_progress")
    agent_services.create_tasks(world["hal"], [{"ext_id": "T11", "title": "Review", "project": "P1",
                                                "parent": "T10", "status": "done"}])
    team = _client(world["sophie"]).get(f"{BASE}/team/").json()
    [idea] = team["ideas"]
    assert idea["stage"] == "started" and [s["ext_id"] for s in idea["subtasks"]] == ["T11"]
    assert [i["ext_id"] for i in team["in_progress"]] == ["T10"] and team["built"] == []


def test_its_owner_marks_an_idea_shipped_and_a_bystander_may_not(world):
    _idea(world, "T10", "track reality at all", status="in_progress")
    assert _act(_client(world["sophie"]), "T10").status_code == 403
    assert _act(_client(world["owner"]), "T10").status_code == 200
    assert [i["ext_id"] for i in _client(world["sophie"]).get(f"{BASE}/team/").json()["built"]] == ["T10"]


def test_a_person_marks_their_own_idea_shipped(world):
    ref = _client(world["sophie"]).post(f"{BASE}/ideas/", data={"quote": "mine"},
                                        content_type="application/json").json()["idea"]["key"]
    agent_services.patch_task(AgentTask.objects.get(key=ref), {"status": "in_progress"})
    assert _act(_client(world["sophie"]), ref).status_code == 200
    assert AgentTask.objects.get(key=ref).status == "done"
    # Not someone else's work, though.
    assert _act(_client(world["sophie"]), "T2").status_code == 403


def test_open_decisions_are_one_line_each_and_say_who_they_wait_on(world):
    hal = world["hal"]
    agent_services.patch_task(agent_services.get_task(hal, "T2"), {"links": [
        {"label": "How many days of ration the forecast assumes (#2386)",
         "url": "https://github.com/x/issues/2386", "kind": "decision"},
        {"label": "Ordinary link", "url": "https://x.test/doc"},
    ]})
    agent_services.create_tasks(hal, [
        {"ext_id": "T4", "title": "Email the participants", "project": "P1",
         "waiting_on_email": "jj@dimagi.com", "next_action": "Approve emailing the Boston participants"},
        {"ext_id": "T5", "title": "[T4] Pick a date", "project": "P1", "ask_kind": "question",
         "ask_body": "Which date suits?\nLong detail the line leaves out."},
        {"ext_id": "T6", "title": "Old", "project": "P1", "status": "done", "ask_kind": "question",
         "ask_body": "A finished task waits on nobody"},
    ])
    team = _client(world["sophie"]).get(f"{BASE}/team/").json()
    assert {(d["text"], d["waiting_on"]) for d in team["waiting"]} == {
        ("How many days of ration the forecast assumes (#2386)", "Jonathan Jackson"),
        ("Approve emailing the Boston participants", "Jonathan Jackson"),
        ("Which date suits?", "Jonathan Jackson"),
    }


def test_the_team_view_says_what_it_is_and_who_may_edit(world):
    viewer = _client(world["sophie"]).get(f"{BASE}/team/").json()
    assert viewer["outcome"] == "One honest record of supply." and viewer["owner_name"] == "Jonathan Jackson"
    assert viewer["links"][0]["url"] == "https://x.test/supply/" and viewer["may_edit"] is False
    assert _client(world["owner"]).get(f"{BASE}/team/").json()["may_edit"] is True


def test_every_tally_agrees(world):
    _idea(world, "T10", "a", status="done")
    _idea(world, "T11", "b", status="in_progress")
    sophie = _client(world["sophie"])
    sophie.post(f"{BASE}/ideas/", data={"quote": "c"}, content_type="application/json")
    team = sophie.get(f"{BASE}/team/").json()
    board = sophie.get(f"{BASE}/contributors/").json()
    jj_id = world["jj"].pk
    page = sophie.get(f"{BASE}/contributors/{jj_id}/").json()
    [jj_team] = [c for c in team["contributors"] if c["id"] == jj_id]
    [jj_board] = [c for c in board["contributors"] if c["id"] == jj_id]
    assert jj_team["ideas"] == jj_board["ideas"] == page["stages"] == {
        "proposed": 0, "started": 1, "shipped": 1, "declined": 0}
    assert team["stages"] == board["stages"] == {"proposed": 1, "started": 1, "shipped": 1, "declined": 0}


def test_a_stranger_gets_404(world):
    outsider = User.objects.create_user("x", "x@dimagi.com", "pw")
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=outsider)
    WorkspaceMembership.objects.create(user=outsider, workspace=other, role=WorkspaceMembership.OWNER)
    c = _client(outsider)
    assert c.get("/api/agents/hal/projects/P1/team/").status_code == 404
    assert c.post("/api/agents/hal/projects/P1/ideas/", data={"quote": "x"},
                  content_type="application/json").status_code == 404
