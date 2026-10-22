"""Choosing which runner a chat turn runs on is one decision,
`canopy_sessions.services.may_place`, asked by every door that chooses: a send
with `placement`, re-pinning a queued turn (`/place`), and moving queued turns
off an offline box (`move_queued_turns`, Slack's move-to-cloud). For a chat with
an agent it is `agents.access.may_pin_runner`; for a project chat, administering
the box."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Session
from apps.harness.models import Runner, RunnerAdmin, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.testing import enrolled

pytestmark = pytest.mark.django_db


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _box(owner, name):
    return enrolled(Runner.objects.create(
        name=name, kind=Runner.CLOUD, host=name, owner=owner, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(),
        capabilities={"sessions": True, "projects": ["canopy-web"]},
    ))


@pytest.fixture
def team():
    alice, bob, carol = _user("alice"), _user("bob"), _user("carol")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=alice)
    for u, role in ((alice, WorkspaceMembership.OWNER), (bob, WorkspaceMembership.EDITOR),
                    (carol, WorkspaceMembership.EDITOR)):
        WorkspaceMembership.objects.create(workspace=ws, user=u, role=role)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=alice)
    box = _box(alice, "alice-box")
    # carol administers alice's box without being an admin of the agent.
    RunnerAdmin.objects.create(runner=box, user=carol)
    return alice, bob, carol, ws, agent, box


def _agent_chat(ws, agent, person):
    return Session.objects.create(workspace=ws, created_by=person, agent=agent)


def test_editor_cannot_repin_an_agent_chats_queued_turn_onto_a_box_they_may_not_pin(team):
    _alice, bob, _carol, ws, agent, box = team
    session = _agent_chat(ws, agent, bob)
    chat.send_message(session=session, text="hi", user=bob, client_id="c1")
    c = Client()
    c.force_login(bob)
    r = c.post(f"/api/canopy-sessions/{session.id}/place",
               data={"placement": str(box.id)}, content_type="application/json")
    assert r.status_code == 422, r.content
    assert not Turn.objects.filter(chat_session=session, pinned_runner=box).exists()


def test_editor_cannot_move_an_agent_chats_queued_turns_onto_a_box_they_may_not_pin(team):
    _alice, bob, _carol, ws, agent, box = team
    session = _agent_chat(ws, agent, bob)
    chat.send_message(session=session, text="hi", user=bob, client_id="c1")
    with pytest.raises(ValueError):
        chat.move_queued_turns(session=session, placement=str(box.id), user=bob)


@pytest.mark.parametrize("who", ["alice", "bob", "carol"])
@pytest.mark.parametrize("kind", ["agent", "project"])
def test_send_place_and_move_agree(team, who, kind):
    """The same (user, session, runner) is allowed at send iff it is allowed at
    place and at move — and that is `may_place`."""
    alice, bob, carol, ws, agent, box = team
    user = {"alice": alice, "bob": bob, "carol": carol}[who]

    def fresh():
        if kind == "agent":
            return Session.objects.create(workspace=ws, created_by=user, agent=agent)
        return Session.objects.create(workspace=ws, created_by=user, project="canopy-web")

    def allowed(fn):
        try:
            fn()
            return True
        except ValueError:
            return False

    s1 = fresh()
    at_send = allowed(lambda: chat.send_message(session=s1, text="hi", user=user,
                                                client_id="a", placement=str(box.id)))
    s2 = fresh()
    chat.send_message(session=s2, text="hi", user=user, client_id="b")
    at_place = allowed(lambda: chat.place_queued_turn(session=s2, placement=str(box.id),
                                                      user=user))
    s3 = fresh()
    chat.send_message(session=s3, text="hi", user=user, client_id="c")
    at_move = allowed(lambda: chat.move_queued_turns(session=s3, placement=str(box.id),
                                                     user=user))
    assert at_send == at_place == at_move == chat.may_place(user, s1, box)
    # alice owns the agent and the box; carol administers the box; bob neither.
    assert at_send is (who != "bob")
