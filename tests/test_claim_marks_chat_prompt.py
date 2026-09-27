"""The runner gets the author line; the database keeps the bare words."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions import authorship
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Session
from apps.harness.models import Runner, RunnerAssignment, Turn
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


def test_claimed_chat_prompt_is_marked_but_stored_bare(paired_runner_client, chat_session_with_agent, owner):
    _msg, turn = chat.send_message(session=chat_session_with_agent, text="ship it", user=owner, client_id="c1")
    resp = paired_runner_client.post(f"/api/harness/runners/{paired_runner_client.runner_id}/claim")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(turn.id)
    author, bare, tid = authorship.parse(body["prompt"])
    assert author["user_id"] == owner.id and bare == "ship it" and tid == turn.id.hex
    assert Turn.objects.get(pk=turn.id).prompt == "ship it"


def test_claimed_non_chat_turn_prompt_is_unmarked(paired_runner_client, agent):
    enq = paired_runner_client.post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "manual", "idempotency_key": "k1"},
        content_type="application/json",
    )
    assert enq.status_code == 201, enq.content

    resp = paired_runner_client.post(f"/api/harness/runners/{paired_runner_client.runner_id}/claim")
    assert resp.status_code == 200
    body = resp.json()
    author, bare, tid = authorship.parse(body["prompt"])
    assert author is None and tid is None
    assert bare == body["prompt"]


def _claim(client) -> dict:
    resp = client.post(f"/api/harness/runners/{client.runner_id}/claim")
    assert resp.status_code == 200, resp.content
    return resp.json()


def test_claimed_email_turn_on_a_chat_session_is_bare(paired_runner_client, agent, owner):
    """An email turn is bound to its thread's chat session AND has an initiator —
    but its prompt is a slash command, which Claude Code runs only from the
    FIRST line. A marker above it would turn `/echo:turn` into prose."""
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


def test_claimed_chat_slash_command_is_bare(paired_runner_client, chat_session_with_agent, owner):
    _msg, turn = chat.send_message(session=chat_session_with_agent, text="/compact", user=owner, client_id="c1")
    body = _claim(paired_runner_client)
    assert body["id"] == str(turn.id)
    assert body["prompt"] == "/compact"


def test_a_marked_claim_carries_a_subject_so_an_old_runner_names_it_sensibly(
        paired_runner_client, chat_session_with_agent, owner):
    """An older laptop runner names a new emdash session from the prompt's first
    line — which is now the author marker. It reads `origin_ref["subject"]`
    first, so the claim supplies the bare first line there (in memory only)."""
    long = "Why is the deploy stuck " + "x" * 200
    _msg, turn = chat.send_message(session=chat_session_with_agent, text=f"{long}\nmore", user=owner,
                                   client_id="c1")
    body = _claim(paired_runner_client)
    assert body["origin_ref"]["subject"] == long[:80]
    assert "subject" not in (Turn.objects.get(pk=turn.id).origin_ref or {})


def test_a_claim_keeps_a_subject_it_already_had(paired_runner_client, chat_session_with_agent, owner):
    _msg, turn = chat.send_message(session=chat_session_with_agent, text="hi", user=owner,
                                   client_id="c1", origin_ref={"subject": "The thread"})
    assert _claim(paired_runner_client)["origin_ref"]["subject"] == "The thread"
