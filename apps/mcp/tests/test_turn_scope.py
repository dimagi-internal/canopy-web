"""A confined session reaches canopy's MCP as `agent ∩ caller` — never as the runner's owner.

Driven with a real caller token through the real verifier and the MOUNTED server.
"""
from __future__ import annotations

import contextlib
from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.utils import timezone
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.harness import caller_tokens, services
from apps.harness.models import CallerToken, Turn
from apps.mcp.auth import CanopyPATVerifier
from apps.mcp.server import mcp
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
DMARC = [{"name": "Authentication-Results",
          "value": "mx.google.com; dkim=pass; dmarc=pass header.from=llo-foo.org"}]
IFACE = {"capabilities": {"ask": {"callers": ["contact", "member"],
                                  "tools": ["Read", "mcp__*canopy-web__who_is_asking"]}}}


@pytest.fixture()
def w():
    op = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=op)
    M.objects.create(user=op, workspace=ws, role=M.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=op, interface=parse(IFACE))
    return {"op": op, "ws": ws, "agent": agent}


def _email_turn(agent, key="e1", thread="t-1"):
    t, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key=key,
                                 origin_ref={"from": "fatima@llo-foo.org", "thread_id": thread,
                                             "headers": DMARC})
    assert t.capability == "ask"
    return t


@contextlib.contextmanager
def as_token(raw):
    access = async_to_sync(CanopyPATVerifier().verify_token)(raw)
    assert access is not None, "the token did not verify"
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield access
    finally:
        auth_context_var.reset(tok)


def _names():
    return sorted(t.name for t in async_to_sync(mcp.list_tools)())


def test_a_contacts_session_sees_only_its_capabilitys_tools_and_is_nobody(w):
    turn = _email_turn(w["agent"])
    with as_token(caller_tokens.mint(turn)) as access:
        assert access.claims["user_id"] is None          # an outside contact is no canopy user
        assert _names() == ["who_is_asking"]


def test_who_is_asking_answers_about_its_own_conversation_only(w):
    turn = _email_turn(w["agent"])
    other = _email_turn(w["agent"], key="e2", thread="t-2")
    with as_token(caller_tokens.mint(turn)):
        env = async_to_sync(mcp.call_tool)("who_is_asking", {"turn_id": str(turn.pk)}).structured_content
        assert env["who"]["contact"]["email"] == "fatima@llo-foo.org"
        with pytest.raises(Exception, match="turn not found"):
            async_to_sync(mcp.call_tool)("who_is_asking", {"turn_id": str(other.pk)})


def test_an_unlisted_tool_is_refused_even_by_name(w):
    turn = _email_turn(w["agent"])
    with as_token(caller_tokens.mint(turn)), pytest.raises(Exception, match="not part of"):
        async_to_sync(mcp.call_tool)("list_insights", {})


def test_a_members_session_runs_as_the_member(w):
    from apps.harness import initiator as who

    mem = User.objects.create_user("mem", "mem@else.org", "pw")
    M.objects.create(user=mem, workspace=w["ws"], role=M.EDITOR)
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_API, idempotency_key="m",
                                 initiator=who.for_user(mem, via="chat", assurance=who.SESSION))
    assert t.capability == "ask"
    with as_token(caller_tokens.mint(t)) as access:
        assert access.claims["user_id"] == mem.pk                 # not the runner's owner


def test_the_token_follows_the_conversations_current_turn(w):
    first = _email_turn(w["agent"], key="a")
    raw = caller_tokens.mint(first)
    second = _email_turn(w["agent"], key="b")                     # same thread → same session
    assert second.chat_session_id == first.chat_session_id
    with as_token(raw) as access:
        assert access.claims["turn_id"] == str(second.pk)
        assert set(access.claims["turn_ids"]) == {str(first.pk), str(second.pk)}


@pytest.mark.parametrize("mutate", ["expired", "long_finished", "garbage", "pat_prefix"])
def test_it_fails_closed(w, mutate):
    turn = _email_turn(w["agent"])
    raw = caller_tokens.mint(turn)
    if mutate == "expired":
        CallerToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    elif mutate == "long_finished":
        Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE,
                                               finished_at=timezone.now() - timedelta(hours=1))
    elif mutate == "garbage":
        raw = "cct_" + "x" * 40
    elif mutate == "pat_prefix":
        raw = raw[4:]
    assert async_to_sync(CanopyPATVerifier().verify_token)(raw) is None


def test_a_full_turn_in_the_conversation_voids_the_token(w):
    """A confined conversation should only hold confined turns; if its current
    turn is not one, the token stands for nothing rather than for everything."""
    turn = _email_turn(w["agent"])
    raw = caller_tokens.mint(turn)
    Turn.objects.filter(pk=turn.pk).update(capability="")
    assert async_to_sync(CanopyPATVerifier().verify_token)(raw) is None


def test_only_the_hash_is_stored(w):
    raw = caller_tokens.mint(_email_turn(w["agent"]))
    assert not CallerToken.objects.filter(token_hash=raw).exists()


def test_the_claim_hands_the_token_to_the_claiming_runner_only(w):
    from django.test import Client

    from apps.harness.models import RunnerAssignment

    c = Client()
    c.force_login(w["op"])
    rid = c.post("/api/harness/runners/", {"name": "b", "kind": "emdash",
                                           "capabilities": {"agents": ["ace"], "sessions": True}},
                 content_type="application/json").json()["id"]
    c.post(f"/api/harness/runners/{rid}/heartbeat", {"active_turn_ids": [], "degraded": False,
                                                    "note": "", "profiles": 1},
           content_type="application/json")
    RunnerAssignment.objects.create(agent=w["agent"], runner_id=rid, rank=0)
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_EMAIL, idempotency_key="nothread",
                                 origin_ref={"from": "fatima@llo-foo.org", "headers": DMARC})
    claim = c.post(f"/api/harness/runners/{rid}/claim").json()
    assert claim["mcp_token"].startswith("cct_")
    listing = c.get("/api/harness/turns/").json()
    assert all("mcp_token" not in r for r in listing)


def test_a_personal_token_is_untouched(w):
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=w["op"], label="t")
    with as_token(raw):
        assert len(_names()) > 5 and "list_insights" in _names()
