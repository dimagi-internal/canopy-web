"""The session a runner makes for a dispatched turn records where it came from.

A turn dispatched from inside a chat (`canopy agent dispatch`, an agent asked to
start the work) records its parent on the TURN. The session is made later, by
the runner, in `record_session` — and used to be a bare runner session: no
parent, and none of the people of the chat that asked for it, so the work never
showed under that chat in /ide.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.canopy_sessions.participants import ensure_participant
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    asker = User.objects.create_user("al", "al@dimagi.com", "pw")
    outsider = User.objects.create_user("zz", "zz@example.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="D", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=asker, workspace=ws, role=WorkspaceMembership.EDITOR)
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=owner)
    box = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, host="", owner=owner,
                                workspace=ws, status=Runner.ONLINE,
                                last_heartbeat_at=timezone.now())
    chat = Session.objects.create(workspace=ws, agent=ada, created_by=owner, title="the ask")
    ensure_participant(chat, owner, SessionParticipant.OWNER)
    ensure_participant(chat, asker, SessionParticipant.EDITOR)
    # Not a workspace member: never pulled in, whatever the parent says.
    ensure_participant(chat, outsider, SessionParticipant.VIEWER)
    return {"owner": owner, "asker": asker, "outsider": outsider, "ws": ws, "ada": ada,
            "box": box, "chat": chat}


def _dispatched(w, key="d1", **kw):
    return Turn.objects.create(agent=w["ada"], origin=Turn.ORIGIN_API, idempotency_key=key,
                               prompt="do the thing", claimed_by=w["box"], status=Turn.RUNNING,
                               enqueued_by=w["owner"], parent_session=w["chat"], **kw)


def _record(w, turn, *, session_key="cli-1", send_turn_id=True):
    c = Client()
    c.force_login(w["owner"])
    body = {"thread_key": f"ada:{turn.pk}", "session_key": session_key, "session_id": session_key,
            "agent_slug": "ada"}
    if send_turn_id:
        body["turn_id"] = str(turn.pk)
    r = c.post(f"/api/harness/runners/{w['box'].pk}/record-session", body,
               content_type="application/json")
    assert r.status_code == 200, r.content
    return Session.objects.get(runner_binding__session_key=session_key)


def test_the_runner_made_session_takes_the_turns_parent_and_its_people(world):
    turn = _dispatched(world)
    s = _record(world, turn)
    assert s.parent_session_id == world["chat"].pk
    roles = dict(s.participants.values_list("user__username", "role"))
    # The asker joins; the dispatcher owns nothing here (a runner session has no
    # creator); someone outside the workspace never joins.
    assert roles == {"al": SessionParticipant.EDITOR}


def test_the_turn_is_found_by_its_thread_key_when_the_runner_names_none(world):
    turn = _dispatched(world, origin_ref={"thread_key": "ada:x"})
    c = Client()
    c.force_login(world["owner"])
    r = c.post(f"/api/harness/runners/{world['box'].pk}/record-session",
               {"thread_key": "ada:x", "session_key": "cli-2", "session_id": "cli-2",
                "agent_slug": "ada"}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert Session.objects.get(runner_binding__session_key="cli-2").parent_session_id == turn.parent_session_id


def test_a_parent_the_dispatcher_cannot_read_lends_no_people(world):
    stranger = User.objects.create_user("st", "st@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=stranger, workspace=world["ws"],
                                       role=WorkspaceMembership.EDITOR)
    turn = _dispatched(world)
    Turn.objects.filter(pk=turn.pk).update(enqueued_by=stranger)
    turn.refresh_from_db()
    s = _record(world, turn)
    assert s.parent_session_id == world["chat"].pk
    assert not s.participants.exists()


def test_a_turn_with_no_parent_makes_a_plain_session(world):
    turn = Turn.objects.create(agent=world["ada"], origin=Turn.ORIGIN_API, idempotency_key="p1",
                               prompt="x", claimed_by=world["box"], status=Turn.RUNNING)
    s = _record(world, turn)
    assert s.parent_session_id is None
    assert not s.participants.exists()
