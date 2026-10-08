"""A FULL-profile turn asked by a colleague reads canopy as the colleague (who-is-asking phase 5).

canopy-web#1332. ACE's `full: [member]` rule gives a workspace member the agent's
whole profile, so their turn was not confined and no caller token was minted: the
session's canopy tools used the runner's PAT and read ACE's private widget chats
as the runner's owner. Driven through the real verifier and the MOUNTED server.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from fastmcp.exceptions import ToolError
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import caller_tokens, claiming, services
from apps.harness import initiator as who
from apps.harness.models import CallerToken, Turn
from apps.mcp.auth import CanopyPATVerifier
from apps.mcp.server import mcp
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
SECRET = "Lilianna's private question about the Q3 figures"


@pytest.fixture()
def w():
    op = User.objects.create_user("op", "op@dimagi.com", "pw")           # the runner's owner
    matt = User.objects.create_user("matt", "matt@dimagi.com", "pw")
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=op)
    M.objects.create(user=op, workspace=ws, role=M.OWNER)
    for u in (matt, lili):
        M.objects.create(user=u, workspace=ws, role=M.VIEWER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=op,
                                 interface=parse({"full": ["member"]}))
    return {"op": op, "matt": matt, "lili": lili, "ws": ws, "agent": agent}


def _asked_by(user, agent, key):
    t, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_API, idempotency_key=key,
                                 prompt="what are the Q3 figures?",
                                 initiator=who.for_user(user, via="slack", assurance=who.SESSION))
    return t


def _private_chat_turn(w):
    """The agent's own thread with someone else (the widget chats ACE initiates):
    no creator, so the agent's admins — the runner's owner among them — read it."""
    session = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=None,
                                     origin=Session.ORIGIN_RUNNER, title="widget chat")
    return Turn.objects.create(chat_session=session, agent=None, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               prompt=SECRET, idempotency_key=uuid.uuid4().hex)


@contextlib.contextmanager
def as_token(raw):
    access = async_to_sync(CanopyPATVerifier().verify_token)(raw)
    assert access is not None, "the token did not verify"
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield access
    finally:
        auth_context_var.reset(tok)


def _call(name, args=None):
    return async_to_sync(mcp.call_tool)(name, args or {}).structured_content


def _listing(args=None):
    out = _call("harness_list_turns", args or {})
    return {r["id"]: r for r in (out.get("result") if isinstance(out, dict) else out)}


# -- who gets a scoped token ------------------------------------------------

def test_a_full_rule_members_turn_is_scoped_and_claims_a_token(w):
    t = _asked_by(w["matt"], w["agent"], "m1")
    assert t.capability == ""                                  # full, not confined
    assert caller_tokens.is_caller_scoped(t)
    assert claiming.issue_credentials(t).mcp_token.startswith("cct_")
    assert CallerToken.objects.get().scoped is True


def test_the_owners_turn_keeps_the_runners_own_credential(w):
    t = _asked_by(w["op"], w["agent"], "o1")
    assert t.capability == ""
    assert not caller_tokens.is_caller_scoped(t)
    assert getattr(claiming.issue_credentials(t), "mcp_token", None) is None
    assert not CallerToken.objects.exists()


def test_an_admins_and_canopys_own_turn_are_not_scoped(w):
    from apps.agents.models import AgentAdmin

    adm = User.objects.create_user("adm", "adm@dimagi.com", "pw")
    M.objects.create(user=adm, workspace=w["ws"], role=M.VIEWER)
    AgentAdmin.objects.create(agent=w["agent"], user=adm)
    assert not caller_tokens.is_caller_scoped(_asked_by(adm, w["agent"], "a1"))
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_API, idempotency_key="s1",
                                 initiator=who.system(via="schedule"))
    assert not caller_tokens.is_caller_scoped(t)


# -- what a scoped token can and cannot read --------------------------------

def test_a_members_session_cannot_read_another_users_chat(w):
    theirs = _private_chat_turn(w)
    mine = _asked_by(w["matt"], w["agent"], "m1")
    raw = caller_tokens.mint(mine, scoped=True)

    # The runner's owner reads it — that is the leak this closes.
    op_raw, _ = PersonalToken.create_for_user(user=w["op"], label="runner")
    with as_token(op_raw):
        assert _listing()[str(theirs.pk)]["prompt"] == SECRET

    with as_token(raw) as access:
        assert access.claims["user_id"] == w["matt"].pk        # the asker, not the runner owner
        rows = _listing()
        assert str(theirs.pk) not in rows or rows[str(theirs.pk)]["prompt"] == ""
        assert SECRET not in str(rows)
        with pytest.raises(ToolError, match="404|not found"):
            _call("read_turn_messages", {"turn_id": str(theirs.pk)})
        with pytest.raises(ToolError, match="404|not found"):
            _call("read_turn_transcript", {"turn_id": str(theirs.pk)})


def test_a_members_session_cannot_read_a_chat_a_colleague_started(w):
    """A colleague's private chat is hers alone — the asker is not her."""
    session = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["lili"],
                                     title="lili's chat")
    theirs = Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                                 prompt=SECRET, idempotency_key=uuid.uuid4().hex)
    raw = caller_tokens.mint(_asked_by(w["matt"], w["agent"], "m1"), scoped=True)
    with as_token(raw), pytest.raises(ToolError, match="404|not found"):
        _call("read_turn_messages", {"turn_id": str(theirs.pk)})


def test_a_members_session_still_reads_its_own_turn(w):
    mine = _asked_by(w["matt"], w["agent"], "m1")
    raw = caller_tokens.mint(mine, scoped=True)
    with as_token(raw):
        assert _listing()[str(mine.pk)]["prompt"] == "what are the Q3 figures?"
        _call("read_turn_messages", {"turn_id": str(mine.pk)})   # own turn: no 404


def test_a_scoped_token_lists_every_tool_but_runs_each_as_the_asker(w):
    raw = caller_tokens.mint(_asked_by(w["matt"], w["agent"], "m1"), scoped=True)
    with as_token(raw):
        names = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert {"harness_list_turns", "read_turn_messages", "who_is_asking"} <= names


# -- the token fails closed -------------------------------------------------

def test_a_scoped_token_follows_the_conversations_current_asker(w):
    """The same session, a later turn by someone else: reads follow THAT asker."""
    first = _asked_by(w["matt"], w["agent"], "m1")
    raw = caller_tokens.mint(first, scoped=True)
    # Lilianna replies in the same conversation.
    Turn.objects.filter(pk=first.pk).update(chat_session=Session.objects.create(
        workspace=w["ws"], agent=w["agent"], created_by=w["matt"], title="thread"), agent=None)
    first.refresh_from_db()
    second = Turn.objects.create(chat_session_id=first.chat_session_id, origin=Turn.ORIGIN_API,
                                 idempotency_key="m2", initiator_kind=who.USER,
                                 initiator_user=w["lili"], capability="")
    CallerToken.objects.update(chat_session_id=first.chat_session_id)
    with as_token(raw) as access:
        assert access.claims["user_id"] == w["lili"].pk
        assert access.claims["turn_id"] == str(second.pk)


def test_an_unscoped_token_never_stands_for_a_full_turn(w):
    """Only a scoped token serves a full-profile turn; a confined token still voids."""
    t = _asked_by(w["matt"], w["agent"], "m1")
    raw = caller_tokens.mint(t)                                  # NOT scoped
    assert async_to_sync(CanopyPATVerifier().verify_token)(raw) is None


def test_a_claim_over_rest_hands_a_members_runner_the_scoped_token(w):
    from django.test import Client

    from apps.harness.models import RunnerAssignment

    c = Client()
    c.force_login(w["op"])
    rid = c.post("/api/harness/runners/", {"name": "b", "kind": "emdash",
                                           "capabilities": {"agents": ["ace"], "sessions": True}},
                 content_type="application/json").json()["id"]
    c.post(f"/api/harness/runners/{rid}/heartbeat", {"active_turn_ids": [], "degraded": False,
                                                    "note": "", "profiles": 3, "envelope": 2},
           content_type="application/json")
    RunnerAssignment.objects.create(agent=w["agent"], runner_id=rid, rank=0)
    _asked_by(w["matt"], w["agent"], "m1")
    claim = c.post(f"/api/harness/runners/{rid}/claim").json()
    assert claim["mcp_token"].startswith("cct_")
