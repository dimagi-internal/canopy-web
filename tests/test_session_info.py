"""GET /api/canopy-sessions/{id}/info — everything canopy stores about a session.

The Session info panel (the chat's ⋯ menu and the /ide row menu) reads this one
route. What it must answer is "where did this session come from": who asked,
through which credential and client, under which parent, on which runner — the
record a runner log line used to be the only place to find.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session, SessionParticipant
from apps.canopy_sessions.participants import ensure_participant
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    sid = User.objects.create_user("sid", "sid@dimagi.com", "pw")
    outsider = User.objects.create_user("zz", "zz@example.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="D", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=sid, workspace=ws, role=WorkspaceMembership.EDITOR)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner)
    box = Runner.objects.create(name="haldimagi-mbp", kind=Runner.EMDASH, host="hal@mbp", owner=owner,
                                workspace=ws, status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    session = Session.objects.create(
        workspace=ws, agent=ace, origin=Session.ORIGIN_RUNNER, title="c-cc-ace-task-d4bf",
        provenance={"credential": {"type": "pat", "id": 47}, "client": "canopy-cli/0.2.735",
                    "ip": "10.0.0.1"},
        parent_claude_session="26a71d4e-9b49-4333-ba8c-ea8a1c697845",
    )
    RunnerBinding.objects.create(session=session, runner=box, session_key="c-cc-ace-task-d4bf",
                                 emdash_project="ace", thread_key="dispatch-ace-b6b2",
                                 host="hal@mbp", transcript_id="d8e1cf52")
    ensure_participant(session, owner, SessionParticipant.OWNER)
    turn = Turn.objects.create(
        agent=ace, origin=Turn.ORIGIN_API, idempotency_key="t1", prompt="CC->ACE | Task: MBW",
        status=Turn.DONE, claimed_by=box, session_key="c-cc-ace-task-d4bf", enqueued_by=sid,
        initiator_user=sid, initiator_kind="user", initiator_via="api", turn_mode="manual",
        provenance={"client": "canopy-cli/0.2.735", "ip": "10.0.0.1"},
    )
    # Same agent, another session's key: never listed here.
    Turn.objects.create(agent=ace, origin=Turn.ORIGIN_API, idempotency_key="t2", prompt="other",
                        session_key="some-other-task")
    return {"owner": owner, "sid": sid, "outsider": outsider, "session": session, "turn": turn}


def _get(user, session):
    c = Client()
    c.force_login(user)
    return c.get(f"/api/canopy-sessions/{session.pk}/info")


def test_info_says_where_the_session_came_from(world):
    r = _get(world["owner"], world["session"])
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["session"]["provenance"]["client"] == "canopy-cli/0.2.735"
    assert body["session"]["parent_claude_session"].startswith("26a71d4e")
    assert body["session"]["turn_origin"] == "api"
    assert body["binding"]["thread_key"] == "dispatch-ace-b6b2"
    assert body["binding"]["runner_name"] == "haldimagi-mbp"
    assert body["binding"]["transcript_id"] == "d8e1cf52"
    assert body["turn_count"] == 1
    (turn,) = body["turns"]
    assert turn["id"] == str(world["turn"].pk)
    assert turn["enqueued_by_email"] == "sid@dimagi.com"
    assert turn["claimed_by_name"] == "haldimagi-mbp"
    assert turn["provenance"]["client"] == "canopy-cli/0.2.735"


def test_the_client_ip_is_never_served(world):
    body = _get(world["owner"], world["session"]).json()
    assert "ip" not in body["session"]["provenance"]
    assert "ip" not in body["turns"][0]["provenance"]


def test_someone_who_cannot_read_the_session_gets_404(world):
    assert _get(world["outsider"], world["session"]).status_code == 404


def test_a_web_chat_with_no_runner_has_no_binding(world):
    chat = Session.objects.create(workspace=world["session"].workspace, agent=world["session"].agent,
                                  created_by=world["owner"], title="hello")
    ensure_participant(chat, world["owner"], SessionParticipant.OWNER)
    body = _get(world["owner"], chat).json()
    assert body["binding"] is None
    assert body["turns"] == [] and body["turn_count"] == 0
    assert body["session"]["created_by"] == "jj@dimagi.com"
