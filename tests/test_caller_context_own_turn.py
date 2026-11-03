"""An agent's own login reads the caller envelope of the turn it is RUNNING (`who_is_asking`).

A person's chat is private to its people, so the agent's own login can never read it
through the content gate; `turn_access.can_read_caller_context` adds the one door the
mid-turn re-read needs: the per-turn login, on the turn it serves, while it runs.
"""
from __future__ import annotations

import contextlib

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import turn_agent_tokens
from apps.harness.models import Runner, Turn, TurnAgentToken
from apps.mcp.server import mcp
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=lili, workspace=ws, role=WorkspaceMembership.EDITOR)
    agents = {}
    for slug in ("trellis", "hal"):
        a = Agent.objects.create(slug=slug, name=slug.title(), workspace=ws, owner=owner)
        a.user = User.objects.create_user(f"{slug}-login", f"{slug}@dimagi-ai.com", "pw")
        a.save(update_fields=["user"])
        WorkspaceMembership.objects.create(user=a.user, workspace=ws, role=WorkspaceMembership.EDITOR)
        agents[slug] = a
    box = Runner.objects.create(name="box", kind=Runner.CLOUD, owner=owner, workspace=ws,
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    return {"ws": ws, "lili": lili, "owner": owner, "box": box, **agents}


def _chat_turn(w, agent, *, sensitive=False, key="t1"):
    meta = {"runner_requirements": ["sensitive_data"]} if sensitive else {}
    chat = Session.objects.create(workspace=w["ws"], created_by=w["lili"], title="c",
                                  agent=agent, metadata=meta)
    return Turn.objects.create(chat_session=chat, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               prompt="secret words", idempotency_key=key, status=Turn.RUNNING,
                               claimed_by=w["box"], initiator_kind="user",
                               initiator_user=w["lili"], initiator_via="session",
                               initiator_assurance="session")


def _login(turn, box) -> str:
    return turn_agent_tokens.issue(turn, box)["token"]


def _get(bearer, turn):
    return Client(HTTP_AUTHORIZATION=f"Bearer {bearer}").get(
        f"/api/harness/turns/{turn.pk}/caller-context")


@contextlib.contextmanager
def _mcp_as(user, token_id):
    claims = {"sub": str(user.pk), "user_id": user.pk, "email": user.email,
              "auth_method": "pat", "token_id": token_id, "credential_type": "pat"}
    tok = auth_context_var.set(AuthenticatedUser(AccessToken(
        token="t", client_id=str(user.pk), scopes=["canopy:user"], claims=claims)))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def _who(turn):
    return async_to_sync(mcp.call_tool)("who_is_asking", {"turn_id": str(turn.pk)}).structured_content


def test_an_agent_reads_the_envelope_of_its_own_running_turn(w):
    turn = _chat_turn(w, w["trellis"])
    r = _get(_login(turn, w["box"]), turn)
    assert r.status_code == 200, r.content
    env = r.json()["envelope"]
    assert env["turn_id"] == str(turn.pk) and env["agent"] == "trellis"
    assert "secret words" not in r.content.decode()           # the envelope, never the prompt


def test_the_mcp_tool_answers_for_the_agents_own_running_turn(w):
    turn = _chat_turn(w, w["trellis"])
    _login(turn, w["box"])
    grant = TurnAgentToken.objects.get(turn=turn)
    with _mcp_as(w["trellis"].user, grant.token_id):
        env = _who(turn)
    assert env["turn_id"] == str(turn.pk)
    assert "secret words" not in str(env)


def test_a_finished_turn_is_refused(w):
    turn = _chat_turn(w, w["trellis"])
    bearer = _login(turn, w["box"])
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)
    assert _get(bearer, turn).status_code == 404


def test_only_the_turn_the_login_serves(w):
    mine = _chat_turn(w, w["trellis"], key="a")
    other = _chat_turn(w, w["trellis"], key="b")
    assert _get(_login(mine, w["box"]), other).status_code == 404


def test_another_agents_login_is_refused(w):
    turn = _chat_turn(w, w["trellis"])
    hals = _chat_turn(w, w["hal"], key="h")
    assert _get(_login(hals, w["box"]), turn).status_code == 404
    with _mcp_as(w["hal"].user, TurnAgentToken.objects.get(turn=hals).token_id):
        with pytest.raises(Exception, match="turn not found"):
            _who(turn)


def test_a_stored_token_of_the_agent_outside_a_turn_is_refused(w):
    turn = _chat_turn(w, w["trellis"])
    raw, row = PersonalToken.create_for_user(user=w["trellis"].user, label="canopy-pat")
    assert _get(raw, turn).status_code == 404
    with _mcp_as(w["trellis"].user, row.pk):
        with pytest.raises(Exception, match="turn not found"):
            _who(turn)


def test_a_sensitive_conversation_is_refused(w):
    turn = _chat_turn(w, w["trellis"], sensitive=True)
    assert _get(_login(turn, w["box"]), turn).status_code == 404


def test_the_person_who_asked_and_the_owner_paths_are_unchanged(w):
    turn = _chat_turn(w, w["trellis"])
    c = Client()
    c.force_login(w["lili"])
    assert c.get(f"/api/harness/turns/{turn.pk}/caller-context").status_code == 200
    c2 = Client()
    c2.force_login(w["owner"])
    # The agent's owner is not in the person's chat: still not found.
    assert c2.get(f"/api/harness/turns/{turn.pk}/caller-context").status_code == 404
