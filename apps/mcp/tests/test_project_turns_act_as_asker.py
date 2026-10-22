"""A project's session reaches canopy as the person who asked.

A project chat (a repo, no agent) used to get no canopy identity at all: no
agent, so no agent PAT, and `is_caller_scoped` refused it a caller token. On a
headless cloud runner the MCP then sent no header, got a 401, and fell back to a
browser sign-in no one could complete (cloud-ec2-2, 2026-10-22). Projects and
agents are both first class: an agent's turn acts as the agent, a project's acts
as its verified asker — what they can see is what it can see.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from fastmcp.exceptions import ToolError
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents import delegations
from apps.agents.models import Agent, RepoIdentity
from apps.canopy_sessions.models import Session
from apps.harness import caller_tokens, claiming, services
from apps.harness import initiator as who
from apps.harness.models import CallerToken, Turn
from apps.mcp.auth import CanopyPATVerifier
from apps.mcp.server import mcp
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
PRIVATE = "a private question in someone else's chat"


@pytest.fixture()
def w():
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    lili = User.objects.create_user("lili", "lili@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    M.objects.create(user=jj, workspace=ws, role=M.OWNER)
    M.objects.create(user=lili, workspace=ws, role=M.EDITOR)
    session = Session.objects.create(workspace=ws, project="canopy-web", created_by=jj,
                                     title="project chat")
    return {"jj": jj, "lili": lili, "ws": ws, "session": session}


def _chat_turn(w, user=None, *, assurance=who.SESSION, key=None):
    kw = {}
    if user is not None:
        kw = {"initiator_kind": who.USER, "initiator_user": user, "initiator_assurance": assurance}
    return Turn.objects.create(chat_session=w["session"], origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               prompt="read the stalled session", idempotency_key=key or uuid.uuid4().hex,
                               **kw)


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


# -- who gets a token ----------------------------------------------------------

def test_a_project_chat_turn_claims_a_token_that_acts_as_its_asker(w):
    t = _chat_turn(w, w["jj"])
    assert t.acting_agent is None
    assert caller_tokens.is_caller_scoped(t)
    raw = claiming.issue_credentials(t).mcp_token
    assert raw.startswith("cct_") and CallerToken.objects.get().scoped is True
    with as_token(raw) as access:
        assert access.claims["user_id"] == w["jj"].pk
        assert access.claims["all_tools"] is True


def test_a_project_dispatch_by_a_person_acts_as_them_too(w):
    t, _ = services.enqueue_turn(project="canopy-web", workspace=w["ws"], origin=Turn.ORIGIN_API,
                                 idempotency_key="d1",
                                 initiator=who.for_user(w["jj"], via="api", assurance=who.PAT))
    assert caller_tokens.is_caller_scoped(t)


def test_a_project_turn_with_no_person_behind_it_gets_no_token(w):
    t, _ = services.enqueue_turn(project="canopy-web", workspace=w["ws"], origin=Turn.ORIGIN_API,
                                 idempotency_key="s1", initiator=who.system(via="schedule"))
    assert not caller_tokens.is_caller_scoped(t)
    assert getattr(claiming.issue_credentials(t), "mcp_token", None) is None


def test_an_unverified_asker_gets_no_token(w):
    t = _chat_turn(w, w["jj"], assurance=who.UNKNOWN)
    assert not caller_tokens.is_caller_scoped(t)


def test_an_inactive_asker_gets_no_token(w):
    w["jj"].is_active = False
    w["jj"].save()
    assert not caller_tokens.is_caller_scoped(_chat_turn(w, w["jj"]))


# -- what it reads -------------------------------------------------------------

def test_it_reads_what_the_asker_can_and_nothing_more(w):
    w["session"] = Session.objects.create(workspace=w["ws"], project="canopy-web",
                                          created_by=w["lili"], title="lili's chat")
    mine = _chat_turn(w, w["lili"])
    theirs_session = Session.objects.create(workspace=w["ws"], project="canopy-web",
                                            created_by=w["jj"], title="jj's own chat")
    theirs = Turn.objects.create(chat_session=theirs_session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                                 prompt=PRIVATE, idempotency_key=uuid.uuid4().hex)
    raw = caller_tokens.mint(mine, scoped=True)
    with as_token(raw) as access:
        assert access.claims["user_id"] == w["lili"].pk
        _call("read_turn_messages", {"turn_id": str(mine.pk)})          # her own: readable
        with pytest.raises(ToolError, match="404|not found"):
            _call("read_turn_messages", {"turn_id": str(theirs.pk)})


# -- it fails closed -----------------------------------------------------------

def test_the_token_follows_the_chats_current_asker(w):
    raw = caller_tokens.mint(_chat_turn(w, w["jj"], key="a"), scoped=True)
    _chat_turn(w, w["lili"], key="b")
    with as_token(raw) as access:
        assert access.claims["user_id"] == w["lili"].pk


def test_a_later_turn_with_no_verified_asker_voids_the_token(w):
    raw = caller_tokens.mint(_chat_turn(w, w["jj"], key="a"), scoped=True)
    _chat_turn(w, w["lili"], assurance=who.UNKNOWN, key="b")
    assert async_to_sync(CanopyPATVerifier().verify_token)(raw) is None


def test_an_unscoped_token_never_stands_for_a_project_turn(w):
    raw = caller_tokens.mint(_chat_turn(w, w["jj"]))
    assert async_to_sync(CanopyPATVerifier().verify_token)(raw) is None


# -- GitHub: a project chat borrows its repo's agent ----------------------------

def test_a_project_chat_borrows_its_repos_identity_agent(w):
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=w["ws"], owner=w["jj"])
    RepoIdentity.objects.create(repo_slug="canopy-web", agent=hal)
    t = _chat_turn(w, w["jj"])
    assert t.project == ""                     # a chat turn's repo is on its session
    assert delegations.turn_agent(t) == hal


def test_a_project_chat_on_an_unmapped_repo_borrows_no_one(w):
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=w["ws"], owner=w["jj"])
    RepoIdentity.objects.create(repo_slug="connect-labs", agent=hal)
    assert delegations.turn_agent(_chat_turn(w, w["jj"])) is None
