"""An agent's own login and a person's private chat with that agent: the turn routes agree.

The people API lets an agent's own login recall what a person said to it
(`people_api.list_person_conversations`). The turn routes follow the chat ACL, which keeps
that login out of the conversation's words. Before, they also hid the turns' existence from
it, so the three routes disagreed: `GET /api/harness/turns/?agent=` listed none of a
person's chat turns, the people API listed them, and `GET /api/harness/turns/{id}` 404'd on
the ids it had just been handed.

Now, for the agent's OWN login only (it is the other party to the conversation):

* the list and the detail show THAT each turn ran, content blanked, with a reason naming
  where its words are read;
* every content route answers 403 with that reason instead of a 404;
* nobody else learns anything new: a co-member, the agent's admin and another agent's
  login still see no such turn.

And the people routes say when the id they were given is a user id, not a person id.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.canopy_sessions.models import Session
from apps.contacts import services as contacts
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.people_brain import _agent, _client, _member

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _agent_memory_on(agents_granted):
    yield


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@example.org", "pw")
    ws = Workspace.objects.create(slug="household", display_name="Household", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    ed = _member(ws, "ed")
    other = _member(ws, "other")
    agent = _agent(ws, "bailey", owner)
    sibling = _agent(ws, "hal", owner)
    chat = Session.objects.create(workspace=ws, created_by=ed, agent=agent, title="ed and bailey")
    turn = Turn.objects.create(chat_session=chat, prompt="what's for dinner?",
                               initiator_user=ed, initiator_kind="user", status=Turn.DONE,
                               result_note="pasta")
    return {"owner": owner, "ws": ws, "ed": ed, "other": other, "agent": agent,
            "sibling": sibling, "chat": chat, "turn": turn}


def _listed(user, **params):
    r = _client(user).get("/api/harness/turns/", params)
    assert r.status_code == 200, r.content
    return {row["id"]: row for row in r.json()}


def test_the_agents_login_lists_a_persons_chat_turns_with_it_content_hidden(world):
    rows = _listed(world["agent"].user, agent="bailey")
    row = rows.get(str(world["turn"].pk))
    assert row is not None, "the agent's own chat turns were dropped from the list"
    assert row["status"] == Turn.DONE
    assert row["content_hidden"] is True
    assert row["prompt"] == "" and row["result_note"] == ""
    assert "/api/people/" in row["content_hidden_reason"]


def test_nobody_else_learns_the_chat_exists(world):
    for who in (world["other"], world["owner"], world["sibling"].user):
        assert str(world["turn"].pk) not in _listed(who, agent="bailey")
        r = _client(who).get(f"/api/harness/turns/{world['turn'].pk}")
        assert r.status_code == 404


def test_the_person_still_reads_their_own_chat_turn(world):
    row = _listed(world["ed"], agent="bailey")[str(world["turn"].pk)]
    assert row["content_hidden"] is False and row["prompt"] == "what's for dinner?"


def test_get_turn_opens_what_the_conversations_route_lists(world):
    person = contacts.person_for(user=world["ed"])
    c = _client(world["agent"].user)
    listed = c.get(f"/api/people/{person.pk}/conversations/", {"agent": "bailey"}).json()
    ids = [row["id"] for row in listed["conversations"]]
    assert ids == [str(world["turn"].pk)]
    for tid in ids:
        r = c.get(f"/api/harness/turns/{tid}")
        assert r.status_code == 200, r.content
        assert r.json()["content_hidden"] is True
        assert "/api/people/" in r.json()["content_hidden_reason"]


def test_content_routes_say_why_instead_of_404(world):
    c = _client(world["agent"].user)
    for path in ("events", "messages"):
        r = c.get(f"/api/harness/turns/{world['turn'].pk}/{path}")
        assert r.status_code == 403, (path, r.content)
        assert "/api/people/" in r.json()["detail"]


def test_a_user_id_on_a_person_route_names_the_person_id(world):
    from apps.contacts.models import Person

    # Push the person ids past the user ids, so the user id names no person known here.
    for i in range(world["ed"].pk + 2):
        Person.objects.create(email=f"filler{i}@nowhere.org")
    person = contacts.person_for(user=world["ed"])
    user_id = world["ed"].pk
    assert person.pk != user_id
    r = _client(world["agent"].user).get(f"/api/people/{user_id}/conversations/", {"agent": "bailey"})
    assert r.status_code == 404
    body = r.json()
    assert "user id" in body["detail"] and f"person {person.pk}" in body["detail"]
    assert body["person_id"] == person.pk


def test_a_stranger_user_id_is_a_plain_404(world):
    stranger = User.objects.create_user("stranger", "stranger@elsewhere.org", "pw")
    from apps.contacts.models import Person

    if Person.objects.filter(pk=stranger.pk).exists():
        pytest.skip("the user id collides with a person's id in this database")
    r = _client(world["agent"].user).get(f"/api/people/{stranger.pk}/conversations/", {"agent": "bailey"})
    assert r.status_code == 404
    assert "user id" not in (r.json().get("detail") or "")
