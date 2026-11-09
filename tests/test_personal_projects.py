"""Personal projects: who reads one, who makes and switches one, and what is refused.

The walk over every GET is `tests/test_personal_project_never_leaks.py`; this pins the
behaviour of the writes, and of the people the rule names.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents import services
from apps.agents.credentials import workspace_vaults
from apps.agents.credentials.zk import ZkError
from apps.agents.models import (
    Agent,
    AgentProject,
    AgentTask,
    ProjectParticipant,
    ProjectShare,
    Vault,
)
from apps.contacts.services import person_for
from apps.dynamic_views.models import DynamicView
from apps.contacts import people
from apps.harness import artifact_origin, auto_debug, turn_access
from apps.harness.models import FailureInvestigation, Turn
from apps.huddles.models import ProjectCheckin
from apps.storyboards.models import Storyboard
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

J = "application/json"
LIST = "/api/workspace-projects/"


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _member(ws, name, role=WorkspaceMembership.EDITOR):
    u = User.objects.create_user(name, f"{name}@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=role)
    return u


@pytest.fixture()
def w():
    wsowner = User.objects.create_user("wsowner", "wsowner@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="acme", display_name="Acme", created_by=wsowner)
    WorkspaceMembership.objects.create(user=wsowner, workspace=ws, role=WorkspaceMembership.OWNER)
    me, mate, boss = _member(ws, "me"), _member(ws, "mate"), _member(ws, "boss", WorkspaceMembership.ADMIN)
    lead_login, member_login = _member(ws, "leadbot"), _member(ws, "memberbot")
    lead = Agent.objects.create(slug="leadbot", name="Lead", workspace=ws, owner=boss, user=lead_login, project_seq=1)
    member = Agent.objects.create(slug="memberbot", name="Member", workspace=ws, owner=boss, user=member_login)
    other = Agent.objects.create(slug="otherbot", name="Other", workspace=ws, owner=boss,
                                 user=_member(ws, "otherbot"))
    project = AgentProject.objects.create(workspace=ws, handle="secret-plan", name="Secret plan", owner_user=me,
                                          agent=lead, ext_id="P1", created_by_agent=lead,
                                          visibility=AgentProject.PERSONAL, repo_slug="secret-repo")
    project.member_agents.add(member)
    task = AgentTask.objects.create(workspace=ws, project=project, title="Hidden task", agent=lead, status=AgentTask.TODO)
    return dict(ws=ws, wsowner=wsowner, me=me, mate=mate, boss=boss, lead=lead, member=member, other=other,
                lead_login=lead_login, member_login=member_login, other_login=other.user, project=project, task=task,
                addr=f"{LIST}acme/secret-plan/")


# ---- reading ----------------------------------------------------------------------


@pytest.mark.parametrize("who", ["me", "lead_login", "member_login"])
def test_its_people_read_it(w, who):
    c = _client(w[who])
    assert c.get(w["addr"]).status_code == 200
    assert c.get(f"/api/tasks/{w['task'].key}/").status_code == 200
    assert "secret-plan" in c.get(LIST).content.decode()
    assert "Hidden task" in c.get("/api/tasks/").content.decode()


@pytest.mark.parametrize("who", ["mate", "boss", "wsowner", "other_login"])
def test_nobody_else_does_not_even_an_admin_or_owner(w, who):
    c = _client(w[who])
    missing = c.get(f"{LIST}acme/no-such-plan/")
    found = c.get(w["addr"])
    assert found.status_code == missing.status_code == 404
    # Not found, the same way: the two differ only in the address they repeat.
    assert found.json()["detail"].replace("secret-plan", "no-such-plan") == missing.json()["detail"]
    assert c.get(f"/api/tasks/{w['task'].key}/").status_code == 404
    assert c.get(f"/api/agents/leadbot/projects/{w['project'].handle}/").status_code == 404
    assert "secret-plan" not in c.get(LIST).content.decode()
    assert "secret-plan" not in c.get("/api/projects/", {"status": ""}).content.decode()
    assert "Hidden task" not in c.get("/api/tasks/").content.decode()
    assert "Hidden task" not in c.get("/api/agents/leadbot/tasks/").content.decode()


def test_taking_part_is_not_reading(w):
    """`ProjectParticipant` is filled automatically, so it must grant nothing."""
    ProjectParticipant.objects.create(project=w["project"], person=person_for(user=w["mate"]), source="manual")
    assert _client(w["mate"]).get(w["addr"]).status_code == 404


def test_being_its_assignee_or_filer_is_not_reading(w):
    """Stored by an older write or a direct one: still no way in."""
    t = AgentTask.objects.create(workspace=w["ws"], project=w["project"], title="Parked", assignee_user=w["mate"],
                                 waiting_on_user=w["mate"], created_by_user=w["mate"], status=AgentTask.TODO)
    c = _client(w["mate"])
    assert c.get(f"/api/tasks/{t.key}/").status_code == 404
    assert "Parked" not in c.get("/api/tasks/", {"waiting": "me"}).content.decode()
    assert "Parked" not in c.get("/api/tasks/", {"assignee": "me"}).content.decode()


def test_the_rule_as_a_query_agrees_with_the_rule_for_a_row(w):
    project = w["project"]
    project.member_agents.add(w["lead"])  # two rows through the join: no duplicates from the rule
    for user, expected in ((w["me"], True), (w["lead_login"], True), (w["member_login"], True),
                           (w["mate"], False), (w["boss"], False), (None, False)):
        rows = list(AgentProject.objects.filter(services.project_read_q(user), pk=project.pk))
        assert rows == ([project] if expected else [])
        assert services.may_read_project(user, project) is expected
    loose = AgentTask.objects.create(workspace=w["ws"], title="Loose", status=AgentTask.TODO)
    as_mate = AgentTask.objects.filter(services.task_read_q(w["mate"]), workspace=w["ws"])
    assert list(as_mate) == [loose]
    assert set(AgentTask.objects.filter(services.task_read_q(w["me"]), workspace=w["ws"])) == {loose, w["task"]}


def test_a_workspace_project_is_unchanged(w):
    open_project = AgentProject.objects.create(workspace=w["ws"], handle="open-plan", name="Open", agent=w["lead"])
    assert open_project.visibility == AgentProject.WORKSPACE
    assert _client(w["mate"]).get(f"{LIST}acme/open-plan/").status_code == 200


def test_the_figures_a_non_reader_sees_leave_it_out(w):
    rows = {r["slug"]: r for r in _client(w["mate"]).get("/api/agent-roster/").json()}
    assert rows["leadbot"]["open_tasks"] == 0
    rows = {r["slug"]: r for r in _client(w["me"]).get("/api/agent-roster/").json()}
    assert rows["leadbot"]["open_tasks"] == 1


# ---- making one -------------------------------------------------------------------


def test_made_on_a_persons_route_it_is_theirs(w):
    made = _client(w["mate"]).post(LIST, {"name": "Mine", "workspace": "acme", "visibility": "personal"}, content_type=J)
    assert made.status_code == 201, made.content
    assert made.json()["visibility"] == "personal"
    assert made.json()["owner_email"] == "mate@dimagi.com"
    assert _client(w["me"]).get(f"{LIST}acme/{made.json()['handle']}/").status_code == 404


def test_the_default_is_workspace(w):
    made = _client(w["mate"]).post(LIST, {"name": "Ours", "workspace": "acme"}, content_type=J)
    assert made.json()["visibility"] == "workspace"


def test_the_agent_route_makes_none(w):
    c = _client(w["boss"])
    assert c.post("/api/agents/leadbot/projects/", {"name": "x", "visibility": "personal"}, content_type=J).status_code == 422
    assert not AgentProject.objects.filter(name="x").exists()


def test_an_agents_login_makes_none_on_the_persons_route_either(w):
    made = _client(w["lead_login"]).post(LIST, {"name": "x", "workspace": "acme", "visibility": "personal"}, content_type=J)
    assert made.status_code == 422


def test_a_person_makes_it_with_agents_on_it(w):
    made = _client(w["boss"]).post(LIST, {"name": "With bots", "workspace": "acme", "visibility": "personal",
                                          "steward": "leadbot", "member_agents": ["otherbot"]}, content_type=J).json()
    assert _client(w["other_login"]).get(f"{LIST}acme/{made['handle']}/").status_code == 200


# ---- switching --------------------------------------------------------------------


def _patch(c, addr, **body):
    return c.patch(addr, body, content_type=J)


@pytest.mark.parametrize("who", ["boss", "lead_login", "member_login"])
def test_only_its_lead_switches_it_back(w, who):
    # Any of these reaches the route (the boss only once it is a workspace project).
    AgentProject.objects.filter(pk=w["project"].pk).update(visibility="workspace")
    assert _patch(_client(w[who]), w["addr"], visibility="personal").status_code == 403
    AgentProject.objects.filter(pk=w["project"].pk).update(visibility="personal")
    if who != "boss":
        assert _patch(_client(w[who]), w["addr"], visibility="workspace").status_code == 403


def test_its_lead_switches_it_both_ways(w):
    c = _client(w["me"])
    assert _patch(c, w["addr"], visibility="workspace").json()["visibility"] == "workspace"
    assert _client(w["mate"]).get(w["addr"]).status_code == 200
    assert _patch(c, w["addr"], visibility="personal").json()["visibility"] == "personal"
    assert _client(w["mate"]).get(w["addr"]).status_code == 404


def test_a_project_with_no_lead_has_nobody_to_switch_it(w):
    AgentProject.objects.filter(pk=w["project"].pk).update(visibility="workspace", owner_user=None)
    assert _patch(_client(w["boss"]), w["addr"], visibility="personal").status_code == 403


@pytest.mark.parametrize("attach, words", [
    (lambda w: ProjectShare.objects.create(project=w["project"], workspace=Workspace.objects.create(
        slug="acme-sub", display_name="Sub", created_by=w["me"], parent=w["ws"])), "shared"),
    (lambda w: ProjectCheckin.objects.create(project=w["project"], name="Weekly"), "check-ins"),
    (lambda w: DynamicView.objects.create(workspace=w["ws"], project=w["project"], slug="v", title="V", code="x"), "dynamic views"),
    (lambda w: Storyboard.objects.create(slug="b", title="B", workspace=w["ws"], agent_project=w["project"]), "storyboards"),
    (lambda w: AgentTask.objects.create(workspace=w["ws"], project=w["project"], title="T", agent=w["other"],
                                        status=AgentTask.TODO), "not on it"),
    (lambda w: AgentTask.objects.create(workspace=w["ws"], project=w["project"], title="T", assignee_user=w["mate"],
                                        status=AgentTask.TODO), "not on it"),
])
def test_going_personal_is_refused_while_something_reaches_past_it(w, attach, words):
    AgentProject.objects.filter(pk=w["project"].pk).update(visibility="workspace")
    attach(w)
    resp = _patch(_client(w["me"]), w["addr"], visibility="personal")
    assert resp.status_code == 409, resp.content
    assert words in resp.json()["detail"]
    w["project"].refresh_from_db()
    assert w["project"].visibility == "workspace"


def test_a_working_agent_may_edit_it_but_not_open_it_to_anyone(w):
    c = _client(w["member_login"])
    assert _patch(c, w["addr"], notes="edited").status_code == 200
    for body in ({"member_agents": ["otherbot"]}, {"owner_email": "mate@dimagi.com"}, {"steward": "otherbot"},
                 {"workspace": "acme", "handle": "secret-plan"}):
        assert c.patch(w["addr"], body, content_type=J).status_code == 403, body


def test_it_keeps_a_person_as_its_lead(w):
    c = _client(w["me"])
    assert _patch(c, w["addr"], owner_email="").status_code == 422
    assert _patch(c, w["addr"], owner_email="leadbot@dimagi.com").status_code == 422
    assert _patch(c, w["addr"], owner_email="mate@dimagi.com").status_code == 200
    assert _client(w["mate"]).get(w["addr"]).status_code == 200
    assert c.get(w["addr"]).status_code == 404


def test_it_does_not_move_where_its_lead_is_not_a_member(w):
    elsewhere = Workspace.objects.create(slug="elsewhere", display_name="E", created_by=w["boss"], parent=w["ws"])
    with pytest.raises(services.ProjectScopeError):
        services.promote_project(w["project"], "secret-plan", elsewhere.slug)


def test_taking_an_agent_off_takes_its_work_off(w):
    t = AgentTask.objects.create(workspace=w["ws"], project=w["project"], title="T", agent=w["member"], status=AgentTask.TODO)
    assert _patch(_client(w["me"]), w["addr"], member_agents=[]).status_code == 200
    t.refresh_from_db()
    assert t.agent_id is None


# ---- what a personal project refuses ----------------------------------------------


def test_refused_on_a_personal_project(w):
    c = _client(w["me"])
    sub = Workspace.objects.create(slug="acme-sub", display_name="Sub", created_by=w["me"], parent=w["ws"])
    assert c.post(f"{w['addr']}shares/", {"workspace": sub.slug}, content_type=J).status_code == 422
    assert c.post("/api/agents/leadbot/projects/P1/checkins/", {"name": "W", "lead_email": "me@dimagi.com", "cron": "0 9 * * 1"},
                  content_type=J).status_code == 422
    assert c.post("/api/dynamic-views/", {"workspace": "acme", "project": "secret-plan", "slug": "v", "title": "V",
                                          "code": "function View(){}"}, content_type=J).status_code == 422
    with pytest.raises(ZkError):
        workspace_vaults.attach(Vault.objects.create(workspace=w["ws"], name="v", kind=Vault.WORKSPACE),
                                project=w["project"], by=w["me"])
    with pytest.raises(services.ProjectScopeError):
        services.share_project(w["project"], sub.slug)


def test_an_artifact_never_links_one(w):
    assert artifact_origin.explicit_project(str(w["project"].pk), user=w["me"], turn=None) is None
    assert artifact_origin.explicit_project("leadbot/P1", user=w["me"], turn=None) is None
    assert artifact_origin.project_out(w["project"]) is None
    board = Storyboard.objects.create(slug="b", title="B", workspace=w["ws"])
    resp = _client(w["boss"]).patch(f"/api/storyboards/{board.slug}", {"agent_project": "leadbot/P1"}, content_type=J)
    assert resp.status_code == 422


def test_a_run_document_never_files_under_one(w):
    resp = _client(w["lead_login"]).post("/api/agent-runs/", {"agent": "leadbot", "project": "P1", "subject": "s",
                                                               "kind": "k"}, content_type=J)
    assert resp.status_code == 404


def test_a_fact_is_never_filed_against_one(w):
    person = person_for(user=w["mate"])
    resp = _client(w["me"]).post(f"/api/people/{person.pk}/facts/", {"workspace": "acme", "kind": "project", "statement": "x",
                                                                      "project_id": w["project"].pk}, content_type=J)
    assert resp.status_code == 400


def test_a_proposal_brings_no_partner_into_one(w):
    resp = _client(w["lead_login"]).post(
        "/api/agents/leadbot/projects/P1/tasks/propose/",
        {"title": "T", "why": "w", "done_when": "d", "partners": ["otherbot"]}, content_type=J)
    assert resp.status_code == 422, resp.content
    assert not w["project"].member_agents.filter(pk=w["other"].pk).exists()


def test_proposing_a_project_never_revises_a_personal_one(w):
    AgentProject.objects.filter(pk=w["project"].pk).update(status=AgentProject.PROPOSED)
    resp = _client(w["lead_login"]).post("/api/agents/leadbot/projects/propose/",
                                         {"name": "Secret plan", "outcome": "o", "why": "w"}, content_type=J)
    assert resp.status_code == 201, resp.content
    assert resp.json()["project"]["ref"] != "P1"


# ---- tasks ------------------------------------------------------------------------


def test_filing_into_it_as_a_non_reader_is_as_if_it_were_not_there(w):
    c = _client(w["mate"])
    hidden = c.post("/api/tasks/", {"title": "x", "workspace": "acme", "project": "acme/secret-plan"}, content_type=J)
    absent = c.post("/api/tasks/", {"title": "x", "workspace": "acme", "project": "acme/no-such-plan"}, content_type=J)
    assert hidden.status_code == absent.status_code == 422
    assert hidden.json()["detail"].replace("secret-plan", "no-such-plan") == absent.json()["detail"]
    assert not w["project"].tasks.filter(title="x").exists()


def test_filing_into_it_as_a_reader_works(w):
    made = _client(w["me"]).post("/api/tasks/", {"title": "x", "project": "acme/secret-plan"}, content_type=J)
    assert made.status_code == 201, made.content
    assert made.json()["project_address"] == "acme/secret-plan"


def test_an_agent_not_on_it_files_nowhere(w):
    """An agent's route files a task whose project it cannot find with no project: the
    work is kept, and the miss shows in the answer."""
    made = _client(w["other_login"]).post("/api/agents/otherbot/tasks/", [{"title": "x", "project": "acme/secret-plan"}],
                                          content_type=J)
    assert made.status_code == 201
    assert made.json()[0]["project_address"] in ("", None)
    assert not w["project"].tasks.filter(title="x").exists()


def test_moving_a_task_into_it_needs_reading_it(w):
    loose = AgentTask.objects.create(workspace=w["ws"], title="Loose", status=AgentTask.TODO)
    assert _client(w["mate"]).patch(f"/api/tasks/{loose.key}/", {"project": "acme/secret-plan"}, content_type=J).status_code == 422
    loose.refresh_from_db()
    assert loose.project_id is None
    assert _client(w["me"]).patch(f"/api/tasks/{loose.key}/", {"project": "acme/secret-plan"}, content_type=J).status_code == 200


def test_a_task_cannot_be_made_a_subtask_of_one_it_cannot_read(w):
    c = _client(w["mate"])
    assert c.post("/api/tasks/", {"title": "x", "workspace": "acme", "parent": w["task"].key}, content_type=J).status_code == 422
    assert not AgentTask.objects.filter(title="x").exists()
    loose = AgentTask.objects.create(workspace=w["ws"], title="Loose", status=AgentTask.TODO)
    resp = c.patch(f"/api/tasks/{loose.key}/", {"parent": w["task"].key}, content_type=J)
    assert resp.status_code == 422
    assert "secret-plan" not in resp.content.decode()


@pytest.mark.parametrize("body", [{"assignee": "mate@dimagi.com"}, {"assignee": "otherbot"}, {"waiting_on_email": "mate@dimagi.com"}])
def test_its_work_goes_only_to_its_people(w, body):
    resp = _client(w["me"]).patch(f"/api/tasks/{w['task'].key}/", body, content_type=J)
    assert resp.status_code == 422, resp.content
    resp = _client(w["me"]).post("/api/tasks/", {"title": "x", "project": "acme/secret-plan", **body}, content_type=J)
    assert resp.status_code == 422, resp.content


def test_a_task_held_by_an_outsider_does_not_move_in(w):
    loose = AgentTask.objects.create(workspace=w["ws"], title="Loose", assignee_user=w["mate"], status=AgentTask.TODO)
    resp = _client(w["me"]).patch(f"/api/tasks/{loose.key}/", {"project": "acme/secret-plan"}, content_type=J)
    assert resp.status_code == 422


def test_no_action_or_relation_reaches_it(w):
    c = _client(w["boss"])
    key = w["task"].key
    assert c.post(f"/api/tasks/{key}/actions", {"action": "reply", "comment": "hi"}, content_type=J).status_code == 404
    assert c.patch(f"/api/tasks/{key}/", {"title": "mine"}, content_type=J).status_code == 404
    [row] = c.post("/api/tasks/bulk", {"keys": [key], "action": {"action": "reply", "comment": "x"}}, content_type=J).json()
    assert row["ok"] is False
    loose = AgentTask.objects.create(workspace=w["ws"], title="Loose", status=AgentTask.TODO)
    assert c.post(f"/api/tasks/{loose.key}/relations/", {"kind": "related", "other": key}, content_type=J).status_code == 404
    # Its own lead cannot point a workspace task at it either: a public page would name it.
    assert _client(w["me"]).post(f"/api/tasks/{loose.key}/relations/", {"kind": "related", "other": key},
                                 content_type=J).status_code == 422
    assert c.post(f"/api/tasks/{loose.key}/actions", {"action": "duplicate", "comment": key}, content_type=J).status_code == 422


# ---- a turn's log -----------------------------------------------------------------


@pytest.fixture()
def turn(w):
    return Turn.objects.create(agent=w["lead"], origin=Turn.ORIGIN_API, prompt="Work the secret plan", raised_from_task=w["task"],
                               report_title="About the secret plan", initiator_user=w["boss"], enqueued_by=w["boss"])


def test_its_turns_logs_are_its_readers(w, turn):
    for who in ("boss", "wsowner", "mate"):
        assert not turn_access.can_read_turn_log(w[who], turn), who
    for who in ("me", "lead_login", "member_login"):
        assert turn_access.can_read_turn_log(w[who], turn), who


def test_a_hidden_turn_keeps_nothing_of_it(w, turn):
    body = _client(w["boss"]).get("/api/agents/leadbot/turns").content.decode()
    assert "secret plan" not in body.lower()
    assert "secret plan" not in _client(w["boss"]).get(f"/api/harness/turns/{turn.pk}").content.decode().lower()
    assert "secret plan" in _client(w["me"]).get(f"/api/harness/turns/{turn.pk}").content.decode().lower()


def test_a_failed_turn_is_not_handed_to_the_debugger(w, turn, settings):
    settings.CANOPY_AUTO_DEBUG = True
    turn.status = Turn.FAILED
    turn.result_note = "Died reading the secret plan"
    turn.save(update_fields=["status", "result_note"])
    auto_debug.on_turn_failed(turn)
    assert not FailureInvestigation.objects.exists()


def test_a_persons_conversations_leave_it_out(w, turn):
    turn.initiator_user = w["mate"]
    turn.save(update_fields=["initiator_user"])
    assert people.conversations(person_for(user=w["mate"]), w["lead"]) == []


def test_a_board_turn_of_a_workspace_project_is_unchanged(w, turn):
    AgentProject.objects.filter(pk=w["project"].pk).update(visibility="workspace")
    fresh = Turn.objects.get(pk=turn.pk)
    assert turn_access.can_read_turn_log(w["boss"], fresh)
