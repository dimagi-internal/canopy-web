"""The claim delivers `Turn.prompt` unchanged — for every turn kind, over both
claim channels.

Was `test_claim_marks_chat_prompt.py`: canopy used to prepend an author marker
to a chat send's prompt at claim (`claiming._mark_author`), so the transcript
that came back would still say who wrote it. That broke on the laptop runner
(the marker's trailing newline was lost typing into emdash as one line) and
was redundant besides — the agent already learns who is asking from the
caller envelope, which never touches the prompt. Attribution moved
server-side (`apps.canopy_sessions.services.persist_transcript_rows`); the
claim now just hands over the bare words, unconditionally.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Session
from apps.harness.models import RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _pair(client) -> str:
    resp = client.post(
        "/api/harness/runners/",
        # session-capable: a chat send targets no specific agent, so only a
        # runner opted into `sessions` may claim it (Runner.session_capable).
        {"name": "jj-mbp", "kind": "emdash", "capabilities": {"agents": ["echo"], "sessions": True}},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    return resp.json()["id"]


def _hb(client, rid):
    resp = client.post(
        f"/api/harness/runners/{rid}/heartbeat",
        {"active_turn_ids": [], "degraded": False, "note": ""},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")


@pytest.fixture()
def workspace(owner):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def agent(workspace):
    return Agent.objects.create(slug="echo", name="Echo", workspace=workspace)


@pytest.fixture()
def chat_session_with_agent(workspace, agent, owner):
    return Session.objects.create(workspace=workspace, agent=agent, created_by=owner, title="t")


@pytest.fixture()
def paired_runner_client(owner, agent):
    client = Client()
    client.force_login(owner)
    rid = _pair(client)
    RunnerAssignment.objects.create(agent=agent, runner_id=rid, rank=0)
    _hb(client, rid)
    client.runner_id = rid
    return client


def _claim(client) -> dict:
    resp = client.post(f"/api/harness/runners/{client.runner_id}/claim")
    assert resp.status_code == 200, resp.content
    return resp.json()


def test_claimed_chat_prompt_is_delivered_unchanged(paired_runner_client, chat_session_with_agent, owner):
    _msg, turn = chat.send_message(session=chat_session_with_agent, text="ship it", user=owner, client_id="c1")
    body = _claim(paired_runner_client)
    assert body["id"] == str(turn.id)
    assert body["prompt"] == "ship it"
    assert Turn.objects.get(pk=turn.id).prompt == "ship it"


def test_claimed_non_chat_turn_prompt_is_unchanged(paired_runner_client, agent):
    enq = paired_runner_client.post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "manual", "idempotency_key": "k1", "prompt": "do the thing"},
        content_type="application/json",
    )
    assert enq.status_code == 201, enq.content

    body = _claim(paired_runner_client)
    assert body["prompt"] == "do the thing"


def test_claimed_email_turn_on_a_chat_session_is_unchanged(paired_runner_client, agent, owner):
    """An email turn is bound to its thread's chat session AND has an
    initiator — but there is no marker at all any more, so this is no longer a
    special case; it is exercised anyway because it once was one."""
    from apps.harness import initiator as who
    from apps.harness import services as harness

    turn, _ = harness.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key="email:1",
        prompt="/echo:turn --thread abc",
        origin_ref={"from": "jj@dimagi.com", "subject": "s", "thread_id": "abc"},
        initiator=who.for_user(owner, via="email", assurance="dmarc"),
    )
    assert turn.chat_session_id is not None and turn.initiator_user_id == owner.id
    body = _claim(paired_runner_client)
    assert body["id"] == str(turn.id)
    assert body["prompt"] == "/echo:turn --thread abc"


def test_claimed_chat_slash_command_is_delivered_unchanged(paired_runner_client, chat_session_with_agent, owner):
    _msg, turn = chat.send_message(session=chat_session_with_agent, text="/compact", user=owner, client_id="c1")
    body = _claim(paired_runner_client)
    assert body["id"] == str(turn.id)
    assert body["prompt"] == "/compact"


def test_the_websocket_claim_payload_matches_the_rest_claim(chat_session_with_agent, owner):
    """A cloud runner claims over its control socket, which builds its answer
    through `claiming.claim_payload` rather than the REST route. Both must
    deliver the exact same prompt."""
    from apps.harness.claiming import claim_payload

    _msg, turn = chat.send_message(session=chat_session_with_agent, text="over the socket",
                                   user=owner, client_id="ws1")
    turn = Turn.objects.select_related("initiator_user", "chat_session").get(pk=turn.pk)
    payload = claim_payload(turn)
    assert payload["prompt"] == "over the socket"
    assert Turn.objects.get(pk=turn.pk).prompt == "over the socket"
