"""A runner fetches a chat's attachments through the TURN it holds.

The runner speaks as its owner, and its owner often cannot read the chat: a
teammate's chat with an agent is visible to its creator and participants
(`canopy_sessions/access.py`). Fetching through the chat's own read route
therefore 404'd every file such a teammate sent — on 2026-10-15 a 29-slide deck
review arrived as text alone, three times, and the agent could not tell why.

What these pin:

- the box holding the turn gets the bytes, whether or not its owner can read the chat;
- any attachment already SENT in that turn's chat, earlier messages included (a
  fresh cloud box has no local copy of turn 1's files when it serves turn 3);
- never another chat's, never someone's unsent draft;
- never for a turn this box does not hold right now (queued, another box's, finished),
  nor once its owner is no longer trusted with the agent;
- every refusal is the same 404 as an id that does not exist.
"""
from __future__ import annotations

import uuid
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.canopy_sessions.models import Attachment, Session
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db

JPG = b"\xff\xd8\xff" + b"0" * 64
STORAGE = "apps.canopy_sessions.attachment_storage"


@pytest.fixture(autouse=True)
def _bucket(settings):
    settings.CANOPY_ATTACHMENTS_BUCKET = "test-bucket"


@pytest.fixture
def agent():
    ws = a_workspace()
    owner = User.objects.create_user("olive", "olive@dimagi.com", "pw")
    wsvc.ensure_member(ws, owner, WorkspaceMembership.OWNER)
    return Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner,
                                repo_url="git@github.com:dimagi-internal/ace.git")


@pytest.fixture
def operator(agent):
    # Pairs the box and is trusted with the agent — but is NOT in the chat below.
    # An editor with an explicit grant, so the grant can be taken away (a
    # workspace owner is an agent admin implicitly).
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    wsvc.ensure_member(agent.workspace, user, WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=agent, user=user)
    return user


@pytest.fixture
def runner(operator, agent):
    r = Runner.objects.create(name="laptop", kind=Runner.EMDASH, owner=operator,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=agent, runner=r, rank=0)
    return r


@pytest.fixture
def chat(agent):
    """A teammate's own chat with the agent: its creator reads it, the box's owner does not."""
    carlos = User.objects.create_user("carlos", "carlos@dimagi.com", "pw")
    wsvc.ensure_member(agent.workspace, carlos, WorkspaceMembership.EDITOR)
    return Session.objects.create(agent=agent, workspace=agent.workspace, created_by=carlos,
                                  origin=Session.ORIGIN_WEB)


def _attach(session, *, sent=True, name="slide-01.jpg"):
    return Attachment.objects.create(
        session=session, uploaded_by=session.created_by, filename=name,
        content_type="image/jpeg", size_bytes=len(JPG),
        storage_key=f"sessions/{session.id}/{uuid.uuid4()}/{name}",
        sent_at=timezone.now() if sent else None,
    )


def _turn(session, *, runner=None, status=Turn.CLAIMED):
    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               prompt="review the deck", idempotency_key=uuid.uuid4().hex,
                               status=status, claimed_by=runner)


def _get(runner, turn, attachment_id):
    c = Client()
    c.force_login(runner.owner)  # the runner speaks as its owner, like claim/heartbeat
    with mock.patch(f"{STORAGE}.get") as get:
        get.return_value = mock.Mock(body=JPG, content_type="image/jpeg")
        resp = c.get(f"/api/harness/runners/{runner.pk}/turns/{turn.pk}"
                     f"/attachments/{attachment_id}/content")
    return resp, get


def test_the_box_holding_the_turn_gets_the_file_its_owner_cannot_read(runner, chat):
    shot = _attach(chat)
    turn = _turn(chat, runner=runner)

    # The bug: the chat's own read route refuses the box's owner …
    c = Client()
    c.force_login(runner.owner)
    with mock.patch(f"{STORAGE}.get"):
        assert c.get(f"/api/canopy-sessions/attachments/{shot.id}/content").status_code == 404

    # … and the turn's route serves it, with the same headers as the browser's.
    resp, get = _get(runner, turn, shot.id)
    assert resp.status_code == 200, resp.content
    assert resp.content == JPG
    assert resp["Content-Type"] == "image/jpeg"
    assert resp["Content-Security-Policy"] == "sandbox"
    assert resp["X-Content-Type-Options"] == "nosniff"
    get.assert_called_once_with(shot.storage_key)


def test_a_file_from_an_earlier_message_in_the_same_chat(runner, chat):
    earlier = _attach(chat, name="slide-12.jpg")
    _turn(chat, runner=runner, status=Turn.DONE)  # the turn it was sent with is over
    now = _turn(chat, runner=runner)

    assert _get(runner, now, earlier.id)[0].status_code == 200


def test_never_another_chats_file(runner, chat, agent):
    other = Session.objects.create(agent=agent, workspace=agent.workspace,
                                   created_by=chat.created_by, origin=Session.ORIGIN_WEB)
    elsewhere = _attach(other)
    turn = _turn(chat, runner=runner)

    resp, get = _get(runner, turn, elsewhere.id)
    assert resp.status_code == 404
    get.assert_not_called()


def test_never_an_unsent_draft(runner, chat):
    draft = _attach(chat, sent=False)
    turn = _turn(chat, runner=runner)

    resp, get = _get(runner, turn, draft.id)
    assert resp.status_code == 404
    get.assert_not_called()


@pytest.mark.parametrize("case", ["queued", "other_runner", "finished"])
def test_never_for_a_turn_this_box_does_not_hold(runner, chat, operator, case):
    shot = _attach(chat)
    other = Runner.objects.create(name="cloud", kind=Runner.CLOUD, owner=operator)
    turn = {
        "queued": lambda: _turn(chat, runner=None, status=Turn.QUEUED),
        "other_runner": lambda: _turn(chat, runner=other),
        "finished": lambda: _turn(chat, runner=runner, status=Turn.DONE),
    }[case]()

    resp, get = _get(runner, turn, shot.id)
    assert resp.status_code == 404
    get.assert_not_called()


def test_not_once_the_boxs_owner_is_no_longer_trusted_with_the_agent(runner, chat, agent):
    shot = _attach(chat)
    turn = _turn(chat, runner=runner)
    AgentAdmin.objects.filter(agent=agent, user=runner.owner).delete()

    resp, get = _get(runner, turn, shot.id)
    assert resp.status_code == 404
    get.assert_not_called()


def test_every_refusal_looks_like_a_missing_file(runner, chat):
    turn = _turn(chat, runner=runner)
    draft = _attach(chat, sent=False)

    missing = _get(runner, turn, uuid.uuid4())[0]
    refused = _get(runner, turn, draft.id)[0]
    assert missing.status_code == refused.status_code == 404
    assert missing.json()["detail"] == refused.json()["detail"]
