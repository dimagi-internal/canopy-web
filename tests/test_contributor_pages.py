"""Ideas and contributor pages (hal T69).

Pinned here:

* every task of a project is an idea, its stage is its status in a contributor's
  words, and "who it came from" is resolved strongest evidence first;
* a contributor page is written by an agent only under the person's record
  switch AND its grant, is read by an agent only under use AND its grant, and is
  always readable by a human member — withholding takes the page-based
  attributions with it;
* a page cannot claim a task that is not on the project, and every quote has a link;
* a stranger to the workspace gets 404, and only the person, the agent's admins or
  the writing agent may delete it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.agents import services as agent_services
from apps.agents.models import AgentProject, AgentTask, ProjectParticipant
from apps.contacts import services as contacts
from apps.contacts.models import Person, PersonAccess
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.test_people_brain import _agent, _client, _human_turn, _member

pytestmark = pytest.mark.django_db

BASE = "/agents/hal/projects/P1/contributors/"


@pytest.fixture()
def world(agents_granted):
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan",
                                     last_name="Jackson")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    sophie = _member(ws, "sophie", first_name="Sophie")
    hal = _agent(ws, "hal", owner)
    project = AgentProject.objects.create(agent=hal, ext_id="P1", name="Supply")
    # Their Persons exist (with memory on), and each has granted hal — through a
    # turn, which is how a grant really comes to exist (conftest grants_on_turn).
    for user in (owner, sophie):
        contacts.person_for(user=user)
        _human_turn(hal, user, f"grant-{user.username}")
    tasks = agent_services.create_tasks(hal, [
        {"ext_id": "T1", "title": "Track every supplier reply", "project": "P1", "status": "done"},
        {"ext_id": "T2", "title": "Stock down to each worker", "project": "P1", "status": "in_progress"},
        {"ext_id": "T3", "title": "Per-org page config", "project": "P1"},
    ])
    return {"owner": owner, "ws": ws, "sophie": sophie, "hal": hal, "project": project,
            "tasks": {t.ext_id: t for t in tasks}}


def _summary(**kw):
    body = {
        "goal": "Make what is really going on in a procurement visible before anything is recommended.",
        "quotes": [{"quote": "the normal case is someone just ignores us",
                    "source_url": "https://canopy.dimagi.com/w/connect/chat/abc",
                    "said_at": "2026-09-27T10:00:00Z", "where": "Supply session, Sep 27"}],
        "ideas": [{"task": "t1", "quote": "track reality at all",
                   "source_url": "https://canopy.dimagi.com/w/connect/chat/abc"},
                  {"task": "T2", "quote": "more granular with days of stock"}],
        "open": [{"text": "Interviews with the rest of the team", "quote": "talk to the team"}],
        "sources": [{"label": "Two-pager", "url": "https://docs.google.com/document/d/x"}],
        "basis": "458 prompts across 22 sessions",
    }
    body.update(kw)
    return body


def _put(client, person, body, ws="connect"):
    return client.put(f"/api/w/{ws}{BASE}{person}/summary/", data=body, content_type="application/json")


def _hal(world):
    return _client(world["hal"].user)


# --- stages and attribution ------------------------------------------------------------


def test_every_task_is_an_idea_with_its_stage(world):
    out = _client(world["sophie"]).get(f"/api/w/connect{BASE}").json()
    stages = {i["ext_id"]: i["stage"] for i in out["ideas"]}
    assert stages == {"T1": "shipped", "T2": "started", "T3": "proposed"}
    assert out["stages"] == {"proposed": 1, "started": 1, "shipped": 1, "declined": 0}


def test_a_task_raised_by_a_humans_turn_came_from_them(world):
    hal, sophie = world["hal"], world["sophie"]
    turn = _human_turn(hal, sophie, "raise")
    agent_services.create_tasks(hal, [{"ext_id": "T4", "title": "Sophie's idea", "project": "P1",
                                       "raised_by": str(turn.pk), "source_url": "https://x.test/s"}])
    out = _client(world["owner"]).get(f"/api/w/connect{BASE}").json()
    [idea] = [i for i in out["ideas"] if i["ext_id"] == "T4"]
    assert [(c["name"], c["via"]) for c in idea["came_from"]] == [("Sophie", "raised_by")]
    [brief] = [c for c in out["contributors"] if c["display_name"] == "Sophie"]
    assert brief["ideas"]["proposed"] == 1 and brief["has_page"] is False


def test_the_tasks_human_why_comes_first_when_it_exists(world, monkeypatch):
    # T68 adds `human_why`; until it lands the field is read defensively.
    task = world["tasks"]["T3"]
    monkeypatch.setattr(AgentTask, "human_why", property(
        lambda self: [{"quote": "pages should be dynamic", "who": "jj@dimagi.com",
                       "source_url": "https://x.test/q", "said_at": "2026-10-08T00:00:00Z"}]
        if self.ext_id == "T3" else []), raising=False)
    _put(_hal(world), "jj@dimagi.com", _summary(ideas=[{"task": "T3", "quote": "page config"}]))
    out = _client(world["owner"]).get(f"/api/w/connect{BASE}").json()
    [idea] = [i for i in out["ideas"] if i["ext_id"] == task.ext_id]
    # One entry per person, and the strongest evidence wins.
    assert [(c["name"], c["via"], c["quote"]) for c in idea["came_from"]] == [
        ("Jonathan Jackson", "human_why", "pages should be dynamic")]


# --- writing a page ----------------------------------------------------------------------


def test_an_agent_writes_a_page_and_its_ideas_carry_their_words(world):
    r = _put(_hal(world), "jj@dimagi.com", _summary())
    assert r.status_code == 200, r.content
    page = r.json()
    assert page["summary_state"] == "written"
    assert page["summary"]["written_by"] == "hal"
    assert page["summary"]["quotes"][0]["quote"] == "the normal case is someone just ignores us"
    # Ideas ordered shipped → started, each with their own words; `t1` resolved to T1.
    assert [(i["ext_id"], i["stage"], i["their_words"]["quote"]) for i in page["ideas"]] == [
        ("T1", "shipped", "track reality at all"), ("T2", "started", "more granular with days of stock")]
    assert page["stages"]["shipped"] == 1 and page["summary"]["open"][0]["text"].startswith("Interviews")
    row = ProjectParticipant.objects.get(person__user=world["owner"])
    assert row.source == "summary" and row.summary_by_agent == world["hal"]


def test_the_board_attributes_ideas_from_a_page(world):
    _put(_hal(world), "jj@dimagi.com", _summary())
    out = _client(world["sophie"]).get(f"/api/w/connect{BASE}").json()
    [t1] = [i for i in out["ideas"] if i["ext_id"] == "T1"]
    assert t1["came_from"][0]["via"] == "contributor_page"
    assert t1["came_from"][0]["name"] == "Jonathan Jackson"
    [jj] = out["contributors"]
    assert jj["has_page"] and jj["goal"].startswith("Make what")
    assert jj["ideas"] == {"proposed": 0, "started": 1, "shipped": 1, "declined": 0}


def test_a_page_cannot_claim_a_task_off_the_project(world):
    r = _put(_hal(world), "jj@dimagi.com", _summary(ideas=[{"task": "T99"}]))
    assert r.status_code == 400 and "T99" in r.json()["detail"]


def test_every_quote_needs_a_link_and_an_http_one(world):
    body = _summary(quotes=[{"quote": "x", "source_url": "javascript:alert(1)"}])
    assert _put(_hal(world), "jj@dimagi.com", body).status_code == 422
    body = _summary(quotes=[{"quote": "x"}])
    assert _put(_hal(world), "jj@dimagi.com", body).status_code == 422


def test_record_off_means_no_agent_writes_the_page(world):
    Person.objects.filter(user=world["owner"]).update(hcp_record_available=False)
    r = _put(_hal(world), "jj@dimagi.com", _summary())
    assert r.status_code == 403 and "learn about them" in r.json()["detail"]
    assert not ProjectParticipant.objects.filter(summary__has_key="goal").exists()
    page = _hal(world).get(f"/api/w/connect{BASE}jj@dimagi.com/").json()
    assert page["may_write"] is False and "learn about them" in page["why_not"]


def test_an_agent_without_the_persons_grant_may_not_write(world):
    from apps.contacts.models import PersonGrant

    PersonGrant.objects.filter(person__user=world["owner"]).delete()
    assert _put(_hal(world), "jj@dimagi.com", _summary()).status_code == 403


def test_another_member_may_not_write_someones_page_but_they_may_write_their_own(world):
    sophie = _client(world["sophie"])
    assert _put(sophie, "jj@dimagi.com", _summary()).status_code == 403
    assert _put(sophie, "sophie@dimagi.com", _summary()).status_code == 200


# --- reading a page ----------------------------------------------------------------------


def test_use_off_withholds_the_page_from_agents_but_not_from_people(world):
    _put(_hal(world), "jj@dimagi.com", _summary())
    Person.objects.filter(user=world["owner"]).update(hcp_use_available=False)
    as_agent = _hal(world).get(f"/api/w/connect{BASE}jj@dimagi.com/").json()
    assert as_agent["summary_state"] == "withheld" and as_agent["summary"] is None
    # The attributions that rest on the page go with it...
    assert as_agent["ideas"] == []
    board = _hal(world).get(f"/api/w/connect{BASE}").json()
    assert all(not i["came_from"] for i in board["ideas"])
    assert board["contributors"][0]["page_withheld"] is True and board["contributors"][0]["goal"] == ""
    # ...while a human member still reads it.
    as_member = _client(world["sophie"]).get(f"/api/w/connect{BASE}jj@dimagi.com/").json()
    assert as_member["summary_state"] == "written"


def test_reading_a_page_is_logged_where_the_person_can_see_it(world):
    _put(_hal(world), "jj@dimagi.com", _summary())
    before = PersonAccess.objects.count()
    person = Person.objects.get(user=world["owner"])
    assert _client(world["sophie"]).get(f"/api/w/connect{BASE}{person.pk}/").status_code == 200
    assert PersonAccess.objects.count() == before + 1


def test_a_stranger_to_the_workspace_gets_404(world):
    outsider = User.objects.create_user("x", "x@dimagi.com", "pw")
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=outsider)
    WorkspaceMembership.objects.create(user=outsider, workspace=other, role=WorkspaceMembership.OWNER)
    c = _client(outsider)
    assert c.get(f"/api{BASE}jj@dimagi.com/").status_code == 404
    assert c.get(f"/api{BASE}").status_code == 404
    # Someone the workspace does not deal with is 404 too, never "exists elsewhere".
    assert _client(world["owner"]).get(f"/api/w/connect{BASE}x@dimagi.com/").status_code == 404


# --- deleting a page ---------------------------------------------------------------------


def test_the_person_deletes_their_page_and_a_bystander_cannot(world):
    _put(_hal(world), "jj@dimagi.com", _summary())
    assert _client(world["sophie"]).delete(f"/api/w/connect{BASE}jj@dimagi.com/summary/").status_code == 403
    assert _client(world["owner"]).delete(f"/api/w/connect{BASE}jj@dimagi.com/summary/").status_code == 204
    row = ProjectParticipant.objects.get(person__user=world["owner"])
    assert row.summary == {} and row.summary_written_at is None
    page = _client(world["owner"]).get(f"/api/w/connect{BASE}jj@dimagi.com/").json()
    assert page["summary_state"] == "not_written"
