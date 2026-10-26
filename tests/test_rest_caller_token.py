"""REST takes a conversation's scoped caller token — only where MCP would (canopy-web#1782).

A project chat's session acts as the person who asked. Its MCP did, through the
caller token; its canopy CLI could not, because REST refused `cct_` tokens and a
scoped session has no PAT to fall back on (`no canopy-web PAT`, cloud-ec2-2,
2026-10-22). REST now accepts the SCOPED kind as its asker — but only on the routes
the MCP server serves as tools, so a session acting for someone reaches nothing over
REST that it could not reach over MCP. Driven through the real middleware stack.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpResponse
from django.test import RequestFactory
from django.utils import timezone

from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import caller_tokens, services
from apps.harness import initiator as who
from apps.harness.models import CallerToken, Turn
from apps.mcp import api_tools
from apps.tokens.middleware import BearerTokenAuthMiddleware
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
DMARC = [{"name": "Authentication-Results",
          "value": "mx.google.com; dkim=pass; dmarc=pass header.from=llo-foo.org"}]


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


def _chat_turn(w, user, *, assurance=who.SESSION, session=None):
    return Turn.objects.create(chat_session=session or w["session"],
                               origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="list my turns",
                               idempotency_key=uuid.uuid4().hex, initiator_kind=who.USER,
                               initiator_user=user, initiator_assurance=assurance)


def _bearer(raw):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _messages(turn):
    return f"/api/harness/turns/{turn.pk}/messages"


# -- it signs in as the asker --------------------------------------------------

def test_a_scoped_token_reads_rest_as_its_asker_and_nothing_more(client, w):
    mine = _chat_turn(w, w["jj"])
    lilis_chat = Session.objects.create(workspace=w["ws"], project="canopy-web",
                                        created_by=w["lili"], title="lili's own chat")
    theirs = _chat_turn(w, w["lili"], session=lilis_chat)
    raw = caller_tokens.mint(mine, scoped=True)
    assert client.get(_messages(mine), **_bearer(raw)).status_code == 200
    assert client.get(_messages(theirs), **_bearer(raw)).status_code == 404


def test_the_canonical_tenant_url_works_too(client, w):
    raw = caller_tokens.mint(_chat_turn(w, w["jj"]), scoped=True)
    assert client.get("/api/w/dimagi/harness/turns/", **_bearer(raw)).status_code == 200


def test_the_request_records_the_caller_token_as_its_credential(w):
    turn = _chat_turn(w, w["jj"])
    raw = caller_tokens.mint(turn, scoped=True)
    seen = {}

    def view(request):
        seen.update(user=request.user, method=request.auth_method, cred=request.auth_credential)
        return HttpResponse()

    request = RequestFactory().get("/api/harness/turns/", **_bearer(raw))
    request.user = AnonymousUser()
    assert BearerTokenAuthMiddleware(view)(request).status_code == 200
    assert seen["user"] == w["jj"] and seen["method"] == "caller_token"
    assert seen["cred"] == {"type": "caller_token", "id": CallerToken.objects.get().pk,
                            "label": f"turn:{turn.pk}", "turn_id": str(turn.pk)}


def _me(client, raw):
    resp = client.get("/api/me/", **_bearer(raw))
    assert resp.status_code == 200, resp.content
    return resp.json()["email"]


def test_it_follows_the_conversations_current_asker(client, w):
    raw = caller_tokens.mint(_chat_turn(w, w["jj"]), scoped=True)
    assert _me(client, raw) == "jj@dimagi.com"
    _chat_turn(w, w["lili"])                     # a later message in the same chat, by Lili
    assert _me(client, raw) == "lili@dimagi.com"


# -- it reaches only what MCP reaches ------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("post", "/api/tokens/"),                       # create_token: minting a PAT
    ("put", "/api/people/me/agent-memory/"),        # set_my_agent_memory: the person's own switch
])
def test_a_route_mcp_leaves_out_is_refused(client, w, method, path):
    assert not api_tools.is_tool_route(method, path)
    raw = caller_tokens.mint(_chat_turn(w, w["jj"]), scoped=True)
    resp = getattr(client, method)(path, data={}, content_type="application/json", **_bearer(raw))
    assert resp.status_code == 403
    assert "serves as tools" in resp.json()["detail"]


def test_a_path_that_is_no_api_operation_fails_closed(client, w):
    raw = caller_tokens.mint(_chat_turn(w, w["jj"]), scoped=True)
    assert client.get("/admin/", **_bearer(raw)).status_code == 403


def test_with_no_route_table_registered_nothing_qualifies(client, w, monkeypatch):
    from apps.tokens import middleware

    assert middleware.caller_token_routes is not None       # the MCP app registered it
    monkeypatch.setattr(middleware, "caller_token_routes", None)
    raw = caller_tokens.mint(_chat_turn(w, w["jj"]), scoped=True)
    assert client.get("/api/me/", **_bearer(raw)).status_code == 403


def test_the_route_table_is_the_mcp_tool_list(w):
    spec = api_tools.tool_spec(api_tools.api_schema())
    for path, item in spec["paths"].items():
        for method in item:
            concrete = path.replace("{", "").replace("}", "")   # any value fills a param
            assert api_tools.is_tool_route(method, concrete), (method, path)


# -- it fails closed -----------------------------------------------------------

def test_a_token_whose_chat_has_no_verified_asker_now_is_refused(client, w):
    raw = caller_tokens.mint(_chat_turn(w, w["jj"]), scoped=True)
    _chat_turn(w, w["lili"], assurance=who.UNKNOWN)
    assert client.get("/api/harness/turns/", **_bearer(raw)).status_code == 401


def test_an_expired_token_is_refused(client, w):
    raw = caller_tokens.mint(_chat_turn(w, w["jj"]), scoped=True)
    CallerToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert client.get("/api/harness/turns/", **_bearer(raw)).status_code == 401


def test_an_unscoped_token_on_a_full_turn_is_refused(client, w):
    raw = caller_tokens.mint(_chat_turn(w, w["jj"]))           # not scoped
    assert client.get("/api/harness/turns/", **_bearer(raw)).status_code == 401


def test_a_confined_token_is_refused(client, w):
    iface = {"capabilities": {"ask": {"callers": ["contact", "member"],
                                      "tools": ["mcp__*canopy-web__harness_list_turns"]}}}
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=w["ws"], owner=w["jj"],
                                 interface=parse(iface))
    turn, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key="e1",
                                    origin_ref={"from": "fatima@llo-foo.org", "thread_id": "t-1",
                                                "headers": DMARC})
    assert turn.capability == "ask"
    raw = caller_tokens.mint(turn)
    assert caller_tokens.resolve(raw) is not None              # MCP would take it…
    assert client.get("/api/harness/turns/", **_bearer(raw)).status_code == 401   # …REST will not


def test_an_unknown_caller_token_is_refused(client, w):
    assert client.get("/api/harness/turns/", **_bearer("cct_" + "x" * 43)).status_code == 401


def test_a_browser_session_keeps_its_own_identity(client, w):
    raw = caller_tokens.mint(_chat_turn(w, w["lili"]), scoped=True)
    client.force_login(w["jj"])
    assert _me(client, raw) == "jj@dimagi.com"


# -- it never mints a credential ----------------------------------------------

def test_a_caller_token_cannot_mint_an_agents_never_expiring_token(client, w):
    """`mint_agent_canopy_token` is an MCP tool, so a caller token reaches it over
    REST; the minting gate is an allowlist (browser session, PAT), so a token that
    lives as long as its conversation cannot trade itself for one that never dies."""
    Agent.objects.create(slug="echo", name="Echo", workspace=w["ws"], owner=w["jj"])
    raw = caller_tokens.mint(_chat_turn(w, w["jj"]), scoped=True)
    resp = client.post("/api/agents/echo/canopy-token", data={},
                       content_type="application/json", **_bearer(raw))
    assert resp.status_code == 403
    assert "caller_token" in resp.json()["detail"]


def _agent_chat_turn(w, user):
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=w["ws"], owner=w["jj"])
    session = Session.objects.create(workspace=w["ws"], agent=agent, created_by=user, title="agent chat")
    return _chat_turn(w, user, session=session)


def test_an_agent_turns_caller_token_reads_but_never_writes_as_its_asker(client, w):
    """An agent driven by someone its owner did not make an admin: the asker granted
    it no act-as-you permission, so it reads as them and any write is refused."""
    turn = _agent_chat_turn(w, w["lili"])
    raw = caller_tokens.mint(turn, scoped=True)
    resp = client.post("/api/harness/turns/", data={}, content_type="application/json", **_bearer(raw))
    assert resp.status_code == 403
    assert "haven't granted it an act-as-you permission" in resp.json()["detail"]
    assert client.get("/api/harness/turns/", **_bearer(raw)).status_code == 200
    # Driving the asker's own open page is not a write it is refused.
    resp = client.post(f"/api/canopy-sessions/{turn.chat_session_id}/page-actions/invoke",
                       data={"name": "selectSkill", "args": {}}, content_type="application/json",
                       **_bearer(raw))
    assert "act-as-you" not in resp.content.decode()


def test_a_repo_chats_caller_token_keeps_its_writes(client, w):
    """A repo chat is the person's own tool, not an agent acting for them."""
    raw = caller_tokens.mint(_chat_turn(w, w["jj"]), scoped=True)
    resp = client.post("/api/harness/turns/", data={}, content_type="application/json", **_bearer(raw))
    assert "act-as-you" not in resp.content.decode()
