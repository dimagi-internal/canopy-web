"""canopy-web as the gateway to a host's MCP server, as the visitor (host grant contract v1 §3).

Driven through the MOUNTED canopy MCP server with a real caller token (CLAUDE.md:
"asserting against the MOUNTED server"), against a real FastMCP server standing
in for the host — reached in-process over ASGI, and checking every request's DPoP
proof the way the contract tells the host to. So a pass here means the proof a
real host would receive verifies, not merely that a function returned.

Most of this file is refusals, each asserting the host was NOT called: an
expired grant must never become a call made some other way.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
from datetime import timedelta

import httpx2
import jwt
import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.utils import timezone
from fastmcp import FastMCP
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.interface import GATEWAY_TOOLS, parse, profile
from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.common.encryption import encrypt_secret
from apps.contacts.models import Contact
from apps.harness import caller_tokens
from apps.harness.models import Turn
from apps.mcp.auth import CanopyPATVerifier
from apps.mcp.models import MCPAuditLog
from apps.mcp.server import mcp
from apps.tokens import client_identity, host_gateway
from apps.tokens.models import AppCredential, AppCredentialAgent, HostGrant
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

RESOURCE = "https://host.test/mcp/"
HOST_TOKEN = "host-at-SECRET"
IFACE = {"capabilities": {
    "connect": {"callers": ["contact"], "sites": ["connect-labs"],
                "ceiling": ["mcp__*connect_labs__marketplace_*"],
                "tools": ["mcp__*canopy-web__who_is_asking"]},
    "ask": {"callers": ["contact"], "tools": ["mcp__*canopy-web__who_is_asking"]},
}}


# --- the host -------------------------------------------------------------------


class HostMCP:
    """A real MCP server behind a middleware that enforces DPoP as the contract
    requires of a host, and records what it saw."""

    def __init__(self):
        self.seen: list[dict] = []
        self.calls: list[str] = []
        server = FastMCP("host")

        @server.tool
        def marketplace_orgs_get() -> dict:
            self.calls.append("marketplace_orgs_get")
            return {"orgs": ["llo-foo"]}

        @server.tool
        def marketplace_rounds_list() -> dict:
            self.calls.append("marketplace_rounds_list")
            return {"rounds": []}

        @server.tool
        def admin_delete_everything() -> dict:
            self.calls.append("admin_delete_everything")
            return {"deleted": True}

        self.inner = server.http_app(path="/mcp/", stateless_http=True, json_response=True)
        self.app = self._verify

    async def _verify(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            self.seen.append(headers)
            error = self._check(scope, headers)
            if error:
                body = error.encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.inner(scope, receive, send)

    def _check(self, scope, headers) -> str:
        auth = headers.get("authorization", "")
        if auth != f"DPoP {HOST_TOKEN}":
            return "bad authorization"
        proof = headers.get("dpop", "")
        try:
            jwk = jwt.get_unverified_header(proof)["jwk"]
            claims = jwt.decode(proof, jwt.PyJWK.from_dict(jwk).key,
                                algorithms=["EdDSA", "ES256"])
        except Exception as exc:  # noqa: BLE001
            return f"bad proof {exc}"
        if jwt.get_unverified_header(proof).get("typ") != "dpop+jwt":
            return "bad typ"
        if client_identity.thumbprint(jwk) != client_identity.dpop_jkt():
            return "proof key is not the bound key"
        if claims["htm"] != scope["method"]:
            return "htm"
        if claims["htu"] != f"https://host.test{scope['path']}":
            return f"htu {claims['htu']}"
        ath = base64.urlsafe_b64encode(hashlib.sha256(HOST_TOKEN.encode()).digest()).decode()
        if claims.get("ath") != ath.rstrip("="):
            return "ath"
        return ""

    def lifespan(self):
        return self.inner.router.lifespan_context(self.inner)


@pytest.fixture()
def host(monkeypatch):
    h = HostMCP()
    monkeypatch.setattr(host_gateway, "_transport_override", httpx2.ASGITransport(app=h.app))
    return h


# --- canopy's side ----------------------------------------------------------------


@pytest.fixture()
def w():
    owner = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner,
                                 interface=parse(IFACE))
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    app.host_issuer = "https://host.test"
    app.host_mcp_resource = RESOURCE
    app.save()
    AppCredentialAgent.objects.create(app=app, agent=agent)
    contact = Contact.objects.create(workspace=ws, app=app, external_id="u-42",
                                     display_name="Gillian", source=Contact.SOURCE_EMBED)
    session = Session.objects.create(workspace=ws, agent=agent, contact=contact,
                                     metadata={"embed_app": "connect-labs"},
                                     page_state={"resource": "labs-marketplace://orgs",
                                                 "backing_tool": "marketplace_orgs_get"})
    turn = Turn.objects.create(chat_session=session, prompt="which orgs?",
                               initiator_contact=contact, initiator_kind="contact",
                               capability="connect")
    return {"owner": owner, "ws": ws, "agent": agent, "app": app, "contact": contact,
            "session": session, "turn": turn}


def _grant(w, **over):
    fields = {"app": w["app"], "subject": "u-42", "contact": w["contact"],
              "access_token_enc": encrypt_secret(HOST_TOKEN), "scope": "marketplace:read",
              "resource": RESOURCE, "dpop_jkt": client_identity.dpop_jkt(),
              "expires_at": timezone.now() + timedelta(minutes=10)}
    fields.update(over)
    return HostGrant.objects.create(**fields)


@contextlib.contextmanager
def as_token(raw):
    access = async_to_sync(CanopyPATVerifier().verify_token)(raw)
    assert access is not None
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield access
    finally:
        auth_context_var.reset(tok)


def _call(host, name, args):
    async def main():
        async with host.lifespan():
            try:
                return True, await mcp.call_tool(name, args)
            except Exception as exc:  # noqa: BLE001 - re-raised outside the task group
                return False, exc
    ok, out = async_to_sync(main)()
    if not ok:
        raise out
    return out


def _visitor(w):
    return as_token(caller_tokens.mint(w["turn"]))


# --- the interface wires the gateway in ---------------------------------------------


def test_a_site_capability_carries_the_gateway_tools_and_its_session_sees_them(w):
    tools = profile(w["agent"], "connect")["tools"]
    assert set(GATEWAY_TOOLS) <= set(tools)
    with _visitor(w):
        names = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert {"site_tools", "site_call"} <= names


# --- the happy path ---------------------------------------------------------------------


def test_site_call_reaches_the_host_as_the_visitor_with_a_valid_dpop_proof(w, host):
    _grant(w)
    with _visitor(w):
        out = _call(host, "site_call", {"tool": "marketplace_orgs_get", "arguments": {}})
    body = out.structured_content
    assert body["is_error"] is False and body["tool"] == "marketplace_orgs_get"
    assert host.calls == ["marketplace_orgs_get"]
    assert host.seen and all(h.get("canopy-actor") == "ace" for h in host.seen)
    # Proofs are single-use: every request of the session carried a fresh one.
    jtis = [jwt.decode(h["dpop"], options={"verify_signature": False})["jti"]
            for h in host.seen]
    assert len(jtis) == len(set(jtis))


def test_site_tools_lists_only_the_page_backing_tool_inside_the_ceiling(w, host):
    _grant(w)
    with _visitor(w):
        out = _call(host, "site_tools", {}).structured_content
    assert [t["name"] for t in out["tools"]] == ["marketplace_orgs_get"]
    assert out["site"] == "connect-labs" and out["scope"] == "marketplace:read"


def test_nothing_about_the_token_is_returned_or_audited(w, host):
    _grant(w)
    with _visitor(w):
        out = _call(host, "site_call", {"tool": "marketplace_orgs_get"})
    assert HOST_TOKEN not in str(out.structured_content)
    rows = list(MCPAuditLog.objects.filter(tool="site_call").values_list("args_summary", "error"))
    assert rows and HOST_TOKEN not in str(rows)


def test_the_claim_response_carries_no_host_credential():
    from apps.harness.schemas import ClaimedTurnOut

    assert "on_behalf_of" not in ClaimedTurnOut.model_fields
    assert not any("host" in f or "grant" in f for f in ClaimedTurnOut.model_fields)


# --- refusals: the host is never called ------------------------------------------------


def _refused(w, host, match, name="site_call", args=None):
    with _visitor(w), pytest.raises(Exception, match=match):
        _call(host, name, args if args is not None else {"tool": "marketplace_orgs_get"})
    assert host.calls == [], "a refused call must not reach the host by any route"


def test_an_expired_grant_is_a_refusal_never_a_fallback(w, host):
    _grant(w, expires_at=timezone.now() - timedelta(seconds=1))
    _refused(w, host, "back on the page")
    assert host.seen == []


def test_no_id_jag_at_arrival_means_no_host_tools(w, host):
    _refused(w, host, "back on the page")
    _refused(w, host, "back on the page", name="site_tools", args={})


def test_a_tool_outside_the_ceiling_is_refused(w, host):
    _grant(w)
    w["session"].page_state = {"backing_tool": "admin_delete_everything"}
    w["session"].save()
    _refused(w, host, "not something you may use", args={"tool": "admin_delete_everything"})


def test_a_tool_the_page_did_not_declare_is_refused(w, host):
    _grant(w)
    _refused(w, host, "not something you may use", args={"tool": "marketplace_rounds_list"})


def test_a_page_that_declares_nothing_unlocks_nothing(w, host):
    _grant(w)
    w["session"].page_state = {}
    w["session"].save()
    _refused(w, host, "not something you may use")


def test_a_conversation_on_another_site_is_refused(w, host):
    _grant(w)
    w["session"].metadata = {"embed_app": "some-other-site"}
    w["session"].save()
    _refused(w, host, "does not let me act for you")


def test_a_capability_that_does_not_name_the_site_is_refused(w, host):
    _grant(w)
    Turn.objects.filter(pk=w["turn"].pk).update(capability="ask")
    w["agent"].interface = parse({"capabilities": {
        "ask": {"callers": ["contact"], "tools": list(GATEWAY_TOOLS)}}})
    w["agent"].save()
    _refused(w, host, "not set up to use")


def test_another_visitors_grant_is_never_used(w, host):
    other = Contact.objects.create(workspace=w["ws"], app=w["app"], external_id="u-99",
                                   source=Contact.SOURCE_EMBED)
    _grant(w, subject="u-99", contact=other)
    _refused(w, host, "back on the page")


def test_a_rotated_dpop_key_invalidates_stored_grants(w, host):
    _grant(w, dpop_jkt="not-the-current-key")
    _refused(w, host, "back on the page")


def test_a_disconnected_site_is_refused(w, host):
    _grant(w)
    AppCredential.objects.filter(pk=w["app"].pk).update(revoked_at=timezone.now())
    _refused(w, host, "does not let me act for you")


def test_a_pat_session_cannot_use_the_gateway(w, host):
    from apps.tokens.models import PersonalToken

    _grant(w)
    raw, _ = PersonalToken.create_for_user(user=w["owner"], label="t")
    with as_token(raw), pytest.raises(Exception, match="visitor's session"):
        _call(host, "site_call", {"tool": "marketplace_orgs_get"})
    assert host.calls == []


def test_a_confined_session_without_the_site_capability_cannot_even_see_it(w, host):
    Turn.objects.filter(pk=w["turn"].pk).update(capability="ask")
    with _visitor(w):
        names = {t.name for t in async_to_sync(mcp.list_tools)()}
        assert "site_call" not in names
        with pytest.raises(Exception, match="not part of"):
            _call(host, "site_call", {"tool": "marketplace_orgs_get"})
    assert host.calls == []


# --- interface parsing --------------------------------------------------------------------


def test_the_interface_parses_sites_and_ceiling_and_refuses_nonsense():
    from apps.agents.interface import InterfaceError

    out = parse(IFACE)["capabilities"]["connect"]
    assert out["sites"] == ["connect-labs"] and out["ceiling"] == ["mcp__*connect_labs__marketplace_*"]
    with pytest.raises(InterfaceError, match="needs `sites:`"):
        parse({"capabilities": {"x": {"callers": ["contact"], "ceiling": ["*"]}}})
    with pytest.raises(InterfaceError, match="not a Connected site name"):
        parse({"capabilities": {"x": {"callers": ["contact"], "sites": ["a b"]}}})
    # `pages:` still parses — ACE's live interface uses it.
    assert parse({"capabilities": {"m": {"callers": ["contact"],
                                         "pages": ["labs-marketplace://*"]}}})


def test_a_conversation_on_the_site_selects_the_site_capability(w):
    from apps.agents.interface import capability_for

    turn = Turn(agent=w["agent"], chat_session=w["session"], prompt="x",
                initiator_contact=w["contact"], initiator_kind="contact")
    assert capability_for(turn, w["agent"]) == "connect"
    w["session"].metadata = {}
    assert capability_for(turn, w["agent"]) == "ask"


@pytest.mark.parametrize("name,expected", [
    ("mcp__connect_labs__marketplace_orgs_get", "marketplace_orgs_get"),
    ("mcp__plugin_x_connect_labs__marketplace_orgs_get", "marketplace_orgs_get"),
    ("connect_labs__marketplace_orgs_get", "marketplace_orgs_get"),
    ("marketplace_orgs_get", "marketplace_orgs_get"),
    ("mcp__*connect_labs__*", "*"),
])
def test_host_tool_names_normalise(name, expected):
    assert host_gateway.host_tool_name(name) == expected
