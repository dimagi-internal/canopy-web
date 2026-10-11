"""The human "why" on a task, kept apart from the AI's rationale (T68).

"Canopy tasks do a decent job of explaining why the AI thinks a task matters,
but they don't trace back to the person who originally wanted it." Every task
can carry what a person actually said: their words, verbatim, who said them
(a real Person where canopy knows them) and where.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentProject, AgentTask, AgentTaskHumanWhy
from apps.contacts.models import Person
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

URL = "/api/agents/hal/tasks/"
DOC = "https://docs.google.com/document/d/X/edit"


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan",
                                    last_name="Jackson")


@pytest.fixture()
def agent(owner):
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    a = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=owner)
    AgentProject.objects.create(agent=a, ext_id="P5", name="Supply")
    return a


@pytest.fixture()
def jonathan(owner):
    return Person.objects.create(email="jj@dimagi.com", user=owner)


@pytest.fixture()
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


def _why(quote="store in tasks what a human had an opinion on directly", **who):
    return {"quote": quote, "who": who or {"email": "jj@dimagi.com"}, "source_url": DOC,
            "said_at": "2026-10-09T13:20:00Z"}


def _create(client, **task):
    task.setdefault("title", "Human why")
    return client.post(URL, [task], content_type="application/json")


def test_create_stores_the_quote_and_links_the_person(client, agent, jonathan):
    r = _create(client, ext_id="T68", human_why=[_why()])

    assert r.status_code == 201, r.content
    (why,) = r.json()[0]["human_why"]
    assert why["quote"] == "store in tasks what a human had an opinion on directly"
    assert why["who"] == {"person_id": jonathan.pk, "email": "jj@dimagi.com",
                          "name": "Jonathan Jackson"}
    assert why["source_url"] == DOC
    assert why["said_at"].startswith("2026-10-09T13:20")
    # Separate from the agent's rationale, which is untouched.
    assert r.json()[0]["rationale"] == ""


def test_someone_canopy_does_not_know_keeps_their_name_and_email(client, agent):
    r = _create(client, human_why=[_why(email="sophie@example.org", name="Sophie")])

    assert r.json()[0]["human_why"][0]["who"] == {"person_id": None, "email": "sophie@example.org",
                                                  "name": "Sophie"}


def test_a_person_known_only_to_another_workspace_is_not_linked(client, agent, owner):
    stranger = User.objects.create_user("x", "x@other.org", "pw")
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=stranger)
    WorkspaceMembership.objects.create(user=stranger, workspace=other, role=WorkspaceMembership.OWNER)
    Person.objects.create(email="x@other.org", user=stranger)

    by_email = _create(client, human_why=[_why(email="x@other.org", name="X")])
    assert by_email.json()[0]["human_why"][0]["who"]["person_id"] is None

    by_id = _create(client, human_why=[_why(person_id=Person.objects.get(email="x@other.org").pk)])
    assert by_id.status_code == 422


def test_patch_replaces_the_list_and_an_empty_list_clears_it(client, agent, jonathan):
    _create(client, ext_id="T68", human_why=[_why("first")])

    r = client.patch(f"{URL}T68/", {"human_why": [_why("a"), _why("b")]},
                     content_type="application/json")
    assert [w["quote"] for w in r.json()["human_why"]] == ["a", "b"]

    # A patch that does not name it leaves it alone.
    r = client.patch(f"{URL}T68/", {"title": "renamed"}, content_type="application/json")
    assert [w["quote"] for w in r.json()["human_why"]] == ["a", "b"]

    r = client.patch(f"{URL}T68/", {"human_why": []}, content_type="application/json")
    assert r.json()["human_why"] == []
    assert not AgentTaskHumanWhy.objects.exists()


def test_a_refused_quote_leaves_the_task_unchanged(client, agent, jonathan):
    _create(client, ext_id="T68", human_why=[_why("kept")])

    r = client.patch(f"{URL}T68/", {"title": "new", "human_why": [_why(person_id=999999)]},
                     content_type="application/json")

    assert r.status_code == 422
    task = AgentTask.objects.get(ext_id="T68")
    assert task.title == "Human why"
    assert [w.quote for w in task.human_whys.all()] == ["kept"]


def test_who_must_name_somebody_and_the_quote_must_exist(client, agent):
    assert _create(client, human_why=[{"quote": "x", "who": {}}]).status_code == 422
    assert _create(client, human_why=[{"quote": "", "who": {"name": "J"}}]).status_code == 422


def test_list_detail_project_and_fleet_routes_all_carry_it(client, agent, jonathan):
    _create(client, ext_id="T68", project="P5", human_why=[_why()])

    for body in (client.get(URL).json()[0], client.get(f"{URL}T68/").json(),
                 client.get("/api/agents/hal/projects/P5/").json()["tasks"][0],
                 client.get("/api/tasks/", {"agent": "hal"}).json()[0]):
        assert body["human_why"][0]["who"]["person_id"] == jonathan.pk


def test_the_list_route_does_not_query_per_task(client, agent, jonathan, django_assert_max_num_queries):
    for i in range(6):
        _create(client, human_why=[_why(f"q{i}")])

    with django_assert_max_num_queries(25):
        assert len(client.get(URL).json()) == 6


def test_a_board_turn_brief_puts_the_persons_words_before_the_agents(agent, jonathan):
    from apps.agents import services
    from apps.harness.dispatch import _task_context

    (task,) = services.create_tasks(agent, [{"title": "t", "rationale": "the agent's guess",
                                             "human_why": [_why("do it this way")]}])
    brief = _task_context(task)

    assert brief.index("Jonathan Jackson said") < brief.index("Agent's why: the agent's guess")
    assert "> do it this way" in brief
