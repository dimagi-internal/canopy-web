"""Proposed projects — multi-player mode for any project an agent proposes
(spec 2026-10-16-work-group-huddles-design.md, layers 1 and 2).

Pinned here:

* proposing creates the project (proposed), its plan steps (suggested), ONE
  decision card and a linked task on each partner's own board; proposing the
  same name again revises in place, marks backings stale and files no second
  card; a declined name comes back only with new evidence;
* the decision card's approve starts the project (partners' tasks move on) and
  its decline archives it with the reason — and only a workspace owner/admin or
  the project's owner may do either; any member may reply, and becomes a
  participant;
* backing: one row per person and per agent, a line unless plainly backing, a
  revision makes rows stale, an agent's stance only from that agent or the
  project's agent;
* a proposal nobody decides in 14 days is archived "not taken up";
* engagement is recorded where each act happens, and the summary reads it back;
* the huddle page lists the projects it proposed, with the same card.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents import engagement, proposals
from apps.agents.models import (
    AgentProject,
    AgentTask,
    EngagementEvent,
    ProjectBacking,
    ProjectParticipant,
)
from apps.contacts import services as contacts
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.test_people_brain import _agent, _client, _member

pytestmark = pytest.mark.django_db

AGENTS = "/api/w/connect/agents"
HUDDLE = "https://canopy.test/w/connect/huddles/work-ai-20261016"


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    editor = _member(ws, "ed", role=WorkspaceMembership.EDITOR, first_name="Ed")
    viewer = _member(ws, "sophie", role=WorkspaceMembership.VIEWER, first_name="Sophie")
    ada, eva, echo = (_agent(ws, s, owner) for s in ("ada", "eva", "echo"))
    for u in (owner, editor, viewer):
        contacts.person_for(user=u)
    return {"owner": owner, "ws": ws, "editor": editor, "viewer": viewer,
            "ada": ada, "eva": eva, "echo": echo}


def _propose(world, **kw):
    body = {"name": "Prompt library", "outcome": "Twenty tested prompts in one doc.",
            "why": "The champions keep rewriting the same prompts.",
            "plan": ["Collect prompts", "Test each"], "partners": ["eva"], "source_url": HUDDLE, **kw}
    return _client(world["ada"].user).post(f"{AGENTS}/ada/projects/propose/", body,
                                           content_type="application/json")


def _act(user, ref, action, comment=""):
    return _client(user).post(f"{AGENTS}/ada/tasks/{ref}/actions",
                              {"action": action, "comment": comment}, content_type="application/json")


def _back(user, stance, line=""):
    return _client(user).put(f"{AGENTS}/ada/projects/P1/backings/me/", {"stance": stance, "line": line},
                             content_type="application/json")


# --- proposing -------------------------------------------------------------------


def test_proposing_files_the_project_its_plan_one_card_and_partner_tasks(world):
    r = _propose(world)
    assert r.status_code == 201, r.content
    out = r.json()
    assert out["created"] is True
    card = out["project"]
    assert (card["ref"], card["status"], card["source_url"]) == ("P1", "proposed", HUDDLE)
    assert card["plan"] == ["Collect prompts", "Test each"]
    assert card["why"].startswith("The champions") and card["decision_open"] is True
    project = AgentProject.objects.get(agent=world["ada"])
    [decision] = project.tasks.filter(ask_kind=AgentTask.ASK_REVIEW)
    assert decision.title == proposals.DECISION_TITLE and "Done when: Twenty" in decision.ask_body
    assert card["decision_task"] == decision.ext_id
    [linked] = AgentTask.objects.filter(agent=world["eva"])
    assert linked.status == "suggested" and linked.source_url.endswith("/agents/ada/projects/P1")
    assert card["partners"][0]["agent"] == "eva"
    assert EngagementEvent.objects.get(kind="proposed").project == project


def test_proposing_the_same_name_revises_it_and_backings_go_stale(world):
    _propose(world)
    assert _back(world["viewer"], "backs").status_code == 200
    r = _propose(world, name="prompt LIBRARY", outcome="Thirty prompts.", plan=["Collect prompts"],
                 partners=["echo"])
    assert r.status_code == 200 and r.json()["created"] is False
    project = AgentProject.objects.get(agent=world["ada"])
    assert project.outcome == "Thirty prompts."
    assert project.tasks.filter(ask_kind=AgentTask.ASK_REVIEW).count() == 1
    assert [t.title for t in proposals.plan_steps(project)] == ["Collect prompts"]
    assert ProjectBacking.objects.get().stale is True
    # The dropped partner's suggestion is declined; the new one gets its own.
    assert AgentTask.objects.get(agent=world["eva"]).status == "declined"
    assert AgentTask.objects.get(agent=world["echo"]).status == "suggested"
    # Saying it again clears stale.
    _back(world["viewer"], "backs")
    assert ProjectBacking.objects.get().stale is False


def test_a_declined_project_comes_back_only_with_new_evidence(world):
    _propose(world)
    card = proposals.decision_card(AgentProject.objects.get())
    assert _act(world["owner"], card.ext_id, "decline", "Not this quarter").status_code == 200
    assert _propose(world).status_code == 409
    r = _propose(world, new_evidence="Three groups asked for it this week.")
    assert r.status_code == 201
    assert "New evidence: Three groups" in r.json()["project"]["why"]


def test_proposing_is_the_agent_or_an_editor_and_partners_must_exist(world):
    body = {"name": "X", "outcome": "y", "why": "z"}
    viewer = _client(world["viewer"]).post(f"{AGENTS}/ada/projects/propose/", body,
                                           content_type="application/json")
    assert viewer.status_code == 403
    assert _propose(world, partners=["nobody"]).status_code == 422
    assert _propose(world, partners=["ada"]).status_code == 422


# --- deciding --------------------------------------------------------------------


def test_approving_the_card_starts_the_project_and_moves_partners(world):
    _propose(world)
    project = AgentProject.objects.get()
    card = proposals.decision_card(project)
    r = _act(world["owner"], card.ext_id, "approve")
    assert r.status_code == 200, r.content
    assert r.json()["turn_ids"]  # the lead's turn to start it
    project.refresh_from_db()
    assert project.status == "active"
    assert AgentTask.objects.get(agent=world["eva"]).status == "in_progress"
    assert EngagementEvent.objects.filter(kind="approved", user=world["owner"], project=project).exists()


def test_declining_archives_it_with_the_reason(world):
    _propose(world)
    project = AgentProject.objects.get()
    _act(world["owner"], proposals.decision_card(project).ext_id, "decline", "We have one already")
    project.refresh_from_db()
    assert project.status == "archived" and "Declined: We have one already" in project.notes
    assert AgentTask.objects.get(agent=world["eva"]).status == "declined"
    assert all(s.status == "declined" for s in proposals.plan_steps(project))


def test_only_an_admin_or_the_projects_owner_decides_but_anyone_replies(world):
    _propose(world)
    project = AgentProject.objects.get()
    ref = proposals.decision_card(project).ext_id
    assert _act(world["editor"], ref, "approve").status_code == 403
    assert _act(world["viewer"], ref, "decline", "no").status_code == 403
    r = _act(world["viewer"], ref, "reply", "Could it cover Hindi prompts?")
    assert r.status_code == 200
    person = contacts.person_for(user=world["viewer"])
    assert ProjectParticipant.objects.get(project=project, person=person).source == "reply"
    assert EngagementEvent.objects.filter(kind="commented", user=world["viewer"]).exists()
    # The project's own owner decides it, whatever their role.
    project.owner_user = world["editor"]
    project.save()
    assert _act(world["editor"], ref, "approve").status_code == 200
    page = _client(world["viewer"]).get(f"{AGENTS}/ada/projects/P1/").json()["proposal"]
    assert page["may_decide"] is False and page["comments"][0]["comment"].startswith("Could it")


# --- backing ---------------------------------------------------------------------


def test_a_person_backs_with_a_line_unless_plainly_backing(world):
    _propose(world)
    assert _back(world["viewer"], "passes").status_code == 422
    r = _back(world["viewer"], "backs_with_changes", "Start with the Hindi group")
    assert r.status_code == 200 and r.json()["mine"] is True
    assert _back(world["viewer"], "backs").json()["stance"] == "backs"  # replaces, one row
    assert ProjectBacking.objects.count() == 1
    person = contacts.person_for(user=world["viewer"])
    assert ProjectParticipant.objects.get(person=person).source == "backing"
    assert EngagementEvent.objects.filter(kind="backed").count() == 2
    assert _client(world["viewer"]).delete(f"{AGENTS}/ada/projects/P1/backings/me/").status_code == 204
    assert not ProjectBacking.objects.exists()


def test_an_agents_stance_comes_from_it_or_the_projects_agent(world):
    _propose(world)
    url = f"{AGENTS}/ada/projects/P1/backings/agents/eva/"
    body = {"stance": "backs_with_changes", "line": "Only if Echo writes it up", "huddle_id": "h1"}
    assert _client(world["echo"].user).put(url, body, content_type="application/json").status_code == 403
    r = _client(world["ada"].user).put(url, body, content_type="application/json")  # the lead files it
    assert r.status_code == 200 and r.json()["backer"] == {
        "kind": "agent", "agent": "eva", "person_id": None, "name": "Eva", "email": ""}
    assert r.json()["source"] == "huddle"
    r = _client(world["eva"].user).put(url, {"stance": "backs"}, content_type="application/json")
    assert r.status_code == 200 and ProjectBacking.objects.get().stance == "backs"
    # An agent's login cannot back as a person.
    assert _back(world["eva"].user, "backs").status_code == 422


# --- expiry ----------------------------------------------------------------------


def test_a_proposal_nobody_decides_in_14_days_is_not_taken_up(world):
    _propose(world)
    project = AgentProject.objects.get()
    assert proposals.expire_stale() == 0
    AgentProject.objects.filter(pk=project.pk).update(proposed_at=timezone.now() - dt.timedelta(days=15))
    assert proposals.expire_stale() == 1
    project.refresh_from_db()
    card = proposals.decision_card(project)
    assert project.status == "archived" and "Not taken up" in project.notes
    assert card.status == "declined" and not card.ask_is_open
    assert EngagementEvent.objects.filter(kind="expired", project=project).exists()
    # Expired, not declined by a person: it may simply be proposed again.
    assert _propose(world).status_code == 201


# --- the report, and the huddle page ---------------------------------------------


def test_the_summary_reports_reactions_decisions_and_runs(world):
    _propose(world)
    project = AgentProject.objects.get()
    _back(world["viewer"], "backs")
    _act(world["owner"], proposals.decision_card(project).ext_id, "approve")
    _propose(world, name="Unloved")
    engagement.record("run_announced", workspace="connect", surface="slack", checkin="9", meta={"run": 7})
    engagement.record("run_confirmed", workspace="connect", surface="slack", checkin="9",
                      user=world["owner"], meta={"run": 7})
    out = _client(world["owner"]).get("/api/w/connect/engagement/summary?days=14").json()
    by_name = {p["name"]: p for p in out["proposals"]}
    assert by_name["Prompt library"]["first_reaction_s"] is not None
    assert by_name["Prompt library"]["decision"] == "approved" and by_name["Prompt library"]["backers"] == 1
    assert [p["name"] for p in out["unanswered"]] == ["Unloved"]
    assert out["runs"]["announced"] == out["runs"]["confirmed"] == 1
    assert out["runs"]["median_confirm_s"] is not None
    assert {p["email"] for p in out["people"]} == {"jj@dimagi.com", "sophie@dimagi.com"}
    assert {"kind": "backed", "surface": "web", "n": 1} in out["counts"]
    only_g = _client(world["owner"]).get("/api/w/connect/engagement/summary?checkin=9").json()
    assert only_g["proposals"] == [] and only_g["runs"]["confirmed"] == 1


def test_the_huddle_page_lists_its_proposed_projects_as_cards(world):
    Turn.objects.create(agent=world["ada"], idempotency_key="anchor", cli_session_id="huddle:x",
                        origin_ref={"kind": "huddle", "huddle": "work-ai-20261016", "leader": "ada",
                                    "members": ["eva"]})
    _propose(world)
    _back(world["viewer"], "backs")
    h = _client(world["owner"]).get("/api/w/connect/huddles/work-ai-20261016").json()
    [card] = h["projects"]
    assert card["name"] == "Prompt library" and card["may_decide"] is True
    assert card["backings"][0]["stance"] == "backs"
    assert h["outputs"] == [] and h["checkin"] is None and h["outcome_count"] == 1
