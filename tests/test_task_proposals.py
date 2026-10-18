"""Proposed tasks — what a project's check-in produces (spec
2026-10-17-project-check-ins-design.md §3).

Pinned here:

* `POST …/projects/{ref}/tasks/propose/` creates ONE `suggested` task in the
  project, its plan steps as child tasks and a linked task on each partner's
  board (201); the same title again revises it in place and marks its backings
  stale (200); a title a person declined comes back only with new evidence (409);
* Start and Decline are the task's own approve and decline — Start puts it in
  progress and gives the project's agent a turn, Decline keeps the reason — and
  only the project's owner, the check-in's lead, or a workspace owner/admin
  may do either;
* backings live on the task (one row per person, per agent), an agent's only
  from that agent or the project's agent;
* a proposed task nobody decides in 14 days is declined "not taken up";
* a task spawns a project (`from_task`, or PATCH `spawned_from`), and both
  sides show it;
* the huddle page renders the run's tasks with the same card; the engagement
  summary reports task proposals beside project ones.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.agents import proposals
from apps.agents.models import AgentProject, AgentTask, EngagementEvent, ProjectBacking
from apps.harness.models import Turn
from apps.huddles.models import CheckinRun, ProjectCheckin
from apps.workspaces.models import WorkspaceMembership
from tests.test_people_brain import _client, _member
from tests.test_project_proposals import world  # noqa: F401

pytestmark = pytest.mark.django_db

AGENTS = "/api/w/connect/agents"
HUDDLE = "https://canopy.test/w/connect/huddles/h1"


@pytest.fixture()
def board(world):  # noqa: F811
    lead = _member(world["ws"], "lee", role=WorkspaceMembership.EDITOR, first_name="Lee")
    type(world["ada"]).objects.filter(pk=world["ada"].pk).update(project_seq=1)
    project = AgentProject.objects.create(agent=world["ada"], ext_id="P1", name="Canopy support",
                                          outcome="People get unstuck fast.", status=AgentProject.ACTIVE)
    checkin = ProjectCheckin.objects.create(project=project, name="Weekly check-in", lead=lead,
                                            agents=["ada", "eva"])
    run = CheckinRun.objects.create(checkin=checkin, project=project, checkin_name=checkin.name,
                                    slot=timezone.now(), status=CheckinRun.RUNNING)
    return {**world, "lead": lead, "project": project, "checkin": checkin, "run": run}


def _propose(b, **kw):
    body = {"title": "Write the FAQ", "why": "The same three questions every week.",
            "done_when": "The FAQ is pinned in the channel.", "plan": ["Collect questions", "Draft"],
            "partners": ["eva"], "source_url": HUDDLE, "run": b["run"].pk, **kw}
    return _client(b["ada"].user).post(f"{AGENTS}/ada/projects/P1/tasks/propose/", body,
                                       content_type="application/json")


def _act(user, ref, action, comment=""):
    return _client(user).post(f"{AGENTS}/ada/tasks/{ref}/actions",
                              {"action": action, "comment": comment}, content_type="application/json")


def _task():
    return AgentTask.objects.get(origin_ref__kind=proposals.CHECKIN_PROPOSAL)


def test_proposing_files_one_suggested_task_with_steps_and_partners(board):
    resp = _propose(board)
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["created"] is True
    card = body["task"]
    assert (card["title"], card["status"], card["done_when"]) == (
        "Write the FAQ", "suggested", "The FAQ is pinned in the channel.")
    assert card["plan"] == ["Collect questions", "Draft"] and card["project_ref"] == "P1"
    assert card["checkin"] == board["checkin"].pk and card["run"] == board["run"].pk and card["huddle_id"] == "h1"
    task = _task()
    assert task.project == board["project"] and task.agent == board["ada"]
    assert [s.title for s in task.subtasks.order_by("position")] == ["Collect questions", "Draft"]
    assert all(s.status == "suggested" and s.project == board["project"] for s in task.subtasks.all())
    [partner] = proposals.task_partner_tasks(task)
    assert partner.agent == board["eva"] and partner.status == "suggested"
    ev = EngagementEvent.objects.get(kind="proposed")
    assert ev.task == task and ev.meta["level"] == "task" and ev.checkin == str(board["checkin"].pk)


def test_the_same_title_revises_in_place_and_stales_backings(board):
    _propose(board)
    task = _task()
    proposals.back(None, task=task, stance="backs", agent=board["eva"])
    resp = _propose(board, title="write the faq", why="Now four questions.", plan=["Draft"])
    assert resp.status_code == 200 and resp.json()["created"] is False
    task.refresh_from_db()
    assert task.rationale == "Now four questions." and ProjectBacking.objects.get(task=task).stale is True
    assert AgentTask.objects.filter(origin_ref__kind=proposals.CHECKIN_PROPOSAL).count() == 1
    assert [s.title for s in task.subtasks.all()] == ["Draft"]


def test_a_declined_title_needs_new_evidence(board):
    _propose(board)
    assert _act(board["owner"], _task().ext_id, "decline", "Not now").status_code == 200
    assert _propose(board).status_code == 409
    resp = _propose(board, new_evidence="Ten people asked this week.")
    assert resp.status_code == 201 and "New evidence" in resp.json()["task"]["why"]


def test_a_run_of_another_project_is_refused(board):
    other = AgentProject.objects.create(agent=board["ada"], ext_id="P2", name="Other")
    run = CheckinRun.objects.create(project=other, slot=timezone.now())
    assert _propose(board, run=run.pk).status_code == 422


def test_start_puts_it_in_progress_and_gives_the_agent_a_turn(board):
    _propose(board)
    task = _task()
    assert _act(board["editor"], task.ext_id, "approve").status_code == 403   # an editor may not decide
    resp = _act(board["lead"], task.ext_id, "approve")                       # the check-in's lead may
    assert resp.status_code == 200, resp.content
    task.refresh_from_db()
    assert task.status == "in_progress"
    assert Turn.objects.filter(agent=board["ada"]).exists()
    [partner] = proposals.task_partner_tasks(task)
    assert partner.status == "in_progress"
    assert EngagementEvent.objects.filter(kind="approved", task=task).exists()


def test_decline_keeps_the_reason_and_declines_what_was_only_suggested(board):
    _propose(board)
    task = _task()
    assert _act(board["owner"], task.ext_id, "decline", "We have a wiki").status_code == 200
    task.refresh_from_db()
    assert task.status == "declined" and "Declined: We have a wiki" in task.notes
    assert set(task.subtasks.values_list("status", flat=True)) == {"declined"}
    assert proposals.task_partner_tasks(task).get().status == "declined"


def test_the_projects_owner_may_decide(board):
    board["project"].owner_user = board["viewer"]
    board["project"].save()
    _propose(board)
    assert _act(board["viewer"], _task().ext_id, "approve").status_code == 200


def test_backings_on_a_task(board):
    _propose(board)
    task = _task()
    url = f"{AGENTS}/ada/projects/P1/tasks/{task.ext_id}/backings/"
    r = _client(board["viewer"]).put(url + "me/", {"stance": "backs_with_changes", "line": "Add Slack tips"},
                                     content_type="application/json")
    assert r.status_code == 200, r.content
    _client(board["viewer"]).put(url + "me/", {"stance": "backs"}, content_type="application/json")
    # The project's agent records a member's co-sign; another agent may not.
    r = _client(board["ada"].user).put(url + "agents/eva/", {"stance": "backs", "huddle_id": "h1"},
                                       content_type="application/json")
    assert r.status_code == 200 and r.json()["source"] == "huddle"
    assert _client(board["echo"].user).put(url + "agents/eva/", {"stance": "passes", "line": "no"},
                                           content_type="application/json").status_code == 403
    rows = _client(board["viewer"]).get(url).json()
    assert [(b["backer"]["kind"], b["stance"]) for b in rows] == [("person", "backs"), ("agent", "backs")]
    assert ProjectBacking.objects.filter(task=task, project__isnull=True).count() == 2
    assert _client(board["viewer"]).delete(url + "me/").status_code == 204
    assert ProjectBacking.objects.filter(task=task).count() == 1


def test_a_proposed_task_lapses_after_two_weeks(board):
    _propose(board)
    task = _task()
    later = timezone.now() + dt.timedelta(days=15)
    assert proposals.expire_stale(later) == 1
    task.refresh_from_db()
    assert task.status == "declined" and proposals.NOT_TAKEN_UP in task.notes
    # Lapsed, not declined by a person: it may simply be proposed again.
    assert _propose(board).status_code == 201


def test_a_task_spawns_a_project_and_both_sides_show_it(board):
    _propose(board)
    task = _task()
    body = {"name": "FAQ bot", "outcome": "A bot answers the FAQ.", "why": "Bigger than a task.",
            "from_task": f"ada/{task.ext_id}"}
    resp = _client(board["ada"].user).post(f"{AGENTS}/ada/projects/propose/", body,
                                           content_type="application/json")
    assert resp.status_code == 201, resp.content
    spawned = resp.json()["project"]["spawned_from"]
    assert spawned["ref"] == task.ext_id and spawned["project_ref"] == "P1"
    assert spawned["project_name"] == "Canopy support"
    card = proposals.task_card(task, board["owner"])
    assert [p["name"] for p in card["spawned"]] == ["FAQ bot"]
    bad = {**body, "name": "Other", "from_task": "ada/T999"}
    assert _client(board["ada"].user).post(f"{AGENTS}/ada/projects/propose/", bad,
                                           content_type="application/json").status_code == 422
    # An existing project is linked afterwards with PATCH.
    other = AgentProject.objects.create(agent=board["eva"], ext_id="P7", name="Prior work")
    r = _client(board["owner"]).patch(f"{AGENTS}/eva/projects/P7/", {"spawned_from": f"ada/{task.ext_id}"},
                                      content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["spawned_from"]["ref"] == task.ext_id
    other.refresh_from_db()
    assert other.spawned_from == task
    got = _client(board["owner"]).get(f"{AGENTS}/eva/projects/P7/").json()
    assert got["spawned_from"]["title"] == "Write the FAQ"


def test_the_huddle_page_renders_the_runs_tasks_as_cards(board):
    Turn.objects.create(agent=board["ada"], idempotency_key="anchor", cli_session_id="huddle:h1",
                        origin_ref={"kind": "huddle", "huddle": "h1", "leader": "ada", "members": ["eva"],
                                    "checkin_run": board["run"].pk})
    _propose(board)
    h = _client(board["owner"]).get("/api/w/connect/huddles/h1").json()
    [card] = h["tasks"]
    assert card["title"] == "Write the FAQ" and card["may_decide"] is True
    assert h["outputs"] == [] and h["outcome_count"] == 1
    assert h["checkin"]["project"]["ref"] == "P1" and h["checkin"]["name"] == "Weekly check-in"


def test_the_summary_reports_task_proposals(board):
    _propose(board)
    _propose(board, title="Unloved")
    task = AgentTask.objects.get(title="Write the FAQ")
    _act(board["lead"], task.ext_id, "approve")
    out = _client(board["owner"]).get("/api/w/connect/engagement/summary?days=14").json()
    by = {p["name"]: p for p in out["task_proposals"]}
    assert by["Write the FAQ"]["decision"] == "approved" and by["Write the FAQ"]["project"] == "P1"
    assert [p["name"] for p in out["unanswered_tasks"]] == ["Unloved"]
    assert out["proposals"] == []
    by_name = _client(board["owner"]).get(
        "/api/w/connect/engagement/summary?checkin=Weekly%20check-in").json()
    assert len(by_name["task_proposals"]) == 2
