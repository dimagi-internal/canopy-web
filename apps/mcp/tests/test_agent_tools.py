"""Each agent's declared interface, served as MCP tools for the caller in front of it.

Driven through the MOUNTED server (`mcp.list_tools` / `mcp.call_tool`): a
provider that is never registered is a surface that does not exist.
"""
from __future__ import annotations

import contextlib

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.interface import parse
from apps.agents.models import Agent, AgentAdmin
from apps.canopy_sessions.models import Session
from apps.harness.models import Turn
from apps.mcp.server import mcp
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()
M = WorkspaceMembership
pytestmark = pytest.mark.django_db

IFACE = {"capabilities": {
    "ask": {"description": "Ask ACE about your programme.", "callers": ["member"]},
    "summarise_opportunity": {"description": "Summarise one opportunity.",
                              "callers": ["member"], "input": {"opportunity_id": "integer"}},
    "admin_only": {"description": "Offered to nobody but admins.", "callers": ["contact"]},
}, "callers_default": "none"}


@contextlib.contextmanager
def as_user(user):
    access = AccessToken(token="t", client_id=str(user.pk), scopes=["canopy:user"],
                         claims={"sub": str(user.pk), "user_id": user.pk, "email": user.email})
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


@pytest.fixture()
def w():
    op = User.objects.create_user("op", "op@dimagi.com", "pw")
    mem = User.objects.create_user("mem", "mem@dimagi.com", "pw")
    out = User.objects.create_user("out", "out@else.org", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=op)
    M.objects.create(user=op, workspace=ws, role=M.EDITOR)
    M.objects.create(user=mem, workspace=ws, role=M.EDITOR)
    Workspace.objects.create(slug="other", display_name="Other", created_by=out)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=op,
                               interface=parse(IFACE))
    Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=op)   # no interface
    return {"op": op, "mem": mem, "out": out, "ws": ws, "ace": ace}


def _names(user):
    with as_user(user):
        return sorted(t.name for t in async_to_sync(mcp.list_tools)() if "__" in t.name)


def _call(user, name, **args):
    with as_user(user):
        return async_to_sync(mcp.call_tool)(name, args).structured_content


# --- the list is computed for the caller ----------------------------------------------

def test_a_member_sees_what_members_are_offered(w):
    assert _names(w["mem"]) == ["ace__ask", "ace__summarise_opportunity"]


def test_the_owner_and_admins_see_every_capability(w):
    assert _names(w["op"]) == ["ace__admin_only", "ace__ask", "ace__summarise_opportunity"]
    AgentAdmin.objects.create(agent=w["ace"], user=w["mem"])
    assert "ace__admin_only" in _names(w["mem"])


def test_an_outsider_sees_nothing(w):
    assert _names(w["out"]) == []


def test_an_agent_without_an_interface_offers_no_tools(w):
    assert not any(n.startswith("hal__") for n in _names(w["op"]))


def test_inputs_become_required_typed_parameters(w):
    with as_user(w["mem"]):
        tool = next(t for t in async_to_sync(mcp.list_tools)() if t.name == "ace__summarise_opportunity")
    assert tool.parameters["properties"]["opportunity_id"] == {"type": "integer"}
    assert "opportunity_id" in tool.parameters["required"]


def test_unpublishing_removes_the_tools(w):
    w["ace"].interface = {}
    w["ace"].save(update_fields=["interface"])
    assert _names(w["mem"]) == []


# --- calling ------------------------------------------------------------------------

def test_ask_is_a_turn_asked_by_the_caller_and_confined_to_the_capability(w):
    got = _call(w["mem"], "ace__ask", message="Which opportunities are behind?", wait_seconds=0)
    session = Session.objects.get(pk=got["conversation_id"])
    turn = Turn.objects.get(pk=got["turn_id"])
    assert session.created_by == w["mem"] and session.agent == w["ace"]
    assert (turn.initiator_user_id, turn.initiator_assurance) == (w["mem"].pk, "pat")
    assert turn.initiator_via == "mcp:ask"
    assert turn.capability == "ask"           # a member is a caller: confined
    assert turn.prompt == "Which opportunities are behind?"


def test_the_owner_asking_runs_full(w):
    got = _call(w["op"], "ace__ask", message="status?", wait_seconds=0)
    assert Turn.objects.get(pk=got["turn_id"]).capability == ""


def test_a_named_capability_carries_its_inputs_and_its_own_profile(w):
    got = _call(w["mem"], "ace__summarise_opportunity", opportunity_id=42, wait_seconds=0)
    turn = Turn.objects.get(pk=got["turn_id"])
    assert turn.capability == "summarise_opportunity"
    assert '"opportunity_id": 42' in turn.prompt


def test_a_wrongly_typed_input_is_refused(w):
    with pytest.raises(Exception, match="must be integer"):
        _call(w["mem"], "ace__summarise_opportunity", opportunity_id="42", wait_seconds=0)


def test_the_reply_comes_back_in_the_same_call(w):
    """Under the stub executor the turn completes inline, writing the same
    `assistant` ledger rows a real runner bridges back."""
    got = _call(w["mem"], "ace__ask", message="hello", wait_seconds=5)
    assert got["status"] == "done"
    assert got["reply"]


def test_a_conversation_continues(w):
    first = _call(w["mem"], "ace__ask", message="one", wait_seconds=0)
    second = _call(w["mem"], "ace__ask", message="two", conversation_id=first["conversation_id"],
                   wait_seconds=0)
    assert second["conversation_id"] == first["conversation_id"]
    assert Turn.objects.filter(chat_session_id=first["conversation_id"]).count() == 2


def test_nobody_can_continue_someone_elses_conversation(w):
    first = _call(w["op"], "ace__ask", message="private", wait_seconds=0)
    with pytest.raises(Exception, match="no such conversation"):
        _call(w["mem"], "ace__ask", message="me too", conversation_id=first["conversation_id"],
              wait_seconds=0)


def test_a_tool_listed_then_unpublished_is_refused_at_call_time(w):
    """Listing is not permission: the call re-authorizes."""
    w["ace"].interface = {}
    w["ace"].save(update_fields=["interface"])
    with as_user(w["mem"]), pytest.raises(Exception):
        async_to_sync(mcp.call_tool)("ace__ask", {"message": "hi"})
    assert not Turn.objects.exists()


# --- agent_reply ---------------------------------------------------------------------

def test_agent_reply_reads_your_conversation(w):
    first = _call(w["mem"], "ace__ask", message="hello", wait_seconds=0)
    got = _call(w["mem"], "agent_reply", conversation_id=first["conversation_id"], wait_seconds=5)
    assert got["turn_id"] == first["turn_id"] and got["status"] == "done"


def test_agent_reply_will_not_read_someone_elses(w):
    first = _call(w["op"], "ace__ask", message="private", wait_seconds=0)
    with pytest.raises(Exception, match="conversation not found"):
        _call(w["mem"], "agent_reply", conversation_id=first["conversation_id"])
