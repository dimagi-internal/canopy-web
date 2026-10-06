"""canopy-web as a HOST of its own MCP, end to end, in process.

`apps/tokens/self_host.py`: agents on canopy's own pages call canopy's own
`/api/mcp/` AS THE VISITOR, through the same host grant contract a connected
site implements. Every step runs the real code on both sides:

1. **Issue.** canopy's own widget mints at `POST /api/embed/token?page=…`; for a
   registered page canopy signs an ID-JAG with its host key (the SDK's
   `issue_id_jag`).
2. **Redeem.** canopy's NORMAL consumer path (`host_grants.redeem`) checks it
   against the `canopy-web` site's JWKS URL, discovers the token endpoint from
   RFC 8414 metadata, and POSTs the jwt-bearer grant with a real client
   assertion and DPoP proof — to the SDK's `GrantHandler`, which authenticates
   canopy against its real published client keys. Answered in-process
   (`self_host.loopback_*`), never skipped.
3. **Call.** `site_call` (the mounted MCP tool) reaches canopy's own MCP through
   `host_gateway`, behind the real `DPoPGate` (`apps/mcp/delegation.py`); the
   tool runs as the visitor, limited to the grant's scope.

The visitor here is a workspace MEMBER, not a contact — canopy's own pages are
visited by its own users — and their token reaches no more than their own ACL.
"""
from __future__ import annotations

import contextlib

import httpx2
import pytest
from asgiref.sync import async_to_sync
from canopy_sdk import contract
from canopy_sdk.keys import generate_private_key, private_pem
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var
from starlette.applications import Starlette
from starlette.routing import Mount

from apps.agents.interface import parse
from apps.agents.models import Agent, AgentTask
from apps.canopy_sessions.models import Session
from apps.common.encryption import decrypt_secret
from apps.harness import caller_tokens
from apps.harness.models import Turn
from apps.mcp import delegation
from apps.mcp.auth import CanopyPATVerifier
from apps.mcp.models import MCPAuditLog
from apps.mcp.server import mcp
from apps.tokens import client_identity, host_gateway, self_host
from apps.tokens.models import AppCredential, AppCredentialAgent, HostGrant
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

BASE = "https://canopy.test"
IFACE = {"capabilities": {
    "workbench": {"callers": ["member"], "sites": ["canopy-web"],
                  "ceiling": ["mcp__*canopy-web__list_*", "mcp__*canopy-web__skill_*"],
                  "tools": ["mcp__*canopy-web__who_is_asking"]},
}}


@pytest.fixture(autouse=True)
def _base():
    cache.clear()
    with override_settings(CANOPY_PUBLIC_BASE_URL=BASE):
        yield
    cache.clear()


@pytest.fixture()
def world():
    owner = User.objects.create_user("op", "op@dimagi.com", "pw")
    visitor = User.objects.create_user("gillian", "gillian@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    other = Workspace.objects.create(slug="elsewhere", display_name="Elsewhere", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=visitor, workspace=ws, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(user=owner, workspace=other, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner, interface=parse(IFACE))
    app = AppCredential.create_credential(name="canopy-web", created_by=owner, workspace=ws)
    # The site configured exactly as the docs say: its JWKS URL is canopy's own
    # host key, its issuer and MCP resource are this deployment.
    app.jwks_url = self_host.jwks_url()
    app.host_issuer = self_host.issuer()
    app.host_mcp_resource = self_host.resource()
    app.show_on_canopy_pages = True
    app.save()
    AppCredentialAgent.objects.create(app=app, agent=agent)
    # One open ask in the visitor's workspace, one in a workspace they are not in:
    # the fleet inbox (`list_items`) must return the first and never the second.
    mine = AgentTask.objects.create(
        agent=agent, ext_id="T1", title="review: visible to gillian", origin="manual",
        ask_kind=AgentTask.ASK_REVIEW, idempotency_key="self-host-mine")
    theirs = AgentTask.objects.create(
        agent=Agent.objects.create(slug="hal", name="Hal", workspace=other, owner=owner),
        ext_id="T1", title="review: NOT visible to gillian", origin="manual",
        ask_kind=AgentTask.ASK_REVIEW, idempotency_key="self-host-theirs")
    return {"owner": owner, "visitor": visitor, "ws": ws, "agent": agent, "app": app,
            "mine": mine, "theirs": theirs}


@pytest.fixture()
def mcp_app():
    """canopy's own MCP, mounted the way `config/asgi.py` mounts it: the real
    DPoP gate in front of the real server (stateless here only so a test can
    enter its lifespan)."""
    inner = mcp.http_app(path="/", transport="streamable-http", stateless_http=True, json_response=True)
    app = Starlette(routes=[Mount("/api/mcp", app=delegation.gate(inner))])
    return inner, app


@pytest.fixture()
def gateway_to_self(mcp_app, monkeypatch):
    _inner, app = mcp_app
    monkeypatch.setattr(host_gateway, "_transport_override", httpx2.ASGITransport(app=app))
    return mcp_app


def _mint(user, page):
    client = Client()
    client.force_login(user)
    return client.post(f"/api/embed/token?page={page}", data={}, content_type="application/json")


@contextlib.contextmanager
def _as_visitor(turn):
    access = async_to_sync(CanopyPATVerifier().verify_token)(caller_tokens.mint(turn))
    assert access is not None
    token = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(token)


def _in_lifespan(inner, fn):
    async def main():
        async with inner.router.lifespan_context(inner):
            try:
                return True, await fn()
            except Exception as exc:  # noqa: BLE001 - re-raised outside the task group
                return False, exc
    ok, out = async_to_sync(main)()
    if not ok:
        raise out
    return out


def _visitor_turn(w, backing="list_items"):
    session = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["visitor"],
                                     metadata={"embed_app": "canopy-web"},
                                     page_state={"resource": "item://", "backing_tool": backing})
    return Turn.objects.create(chat_session=session, prompt="which are stale?",
                               initiator_user=w["visitor"], initiator_kind="member",
                               capability="workbench")


def _ctx(raw, ceiling=("*",)):
    return host_gateway.SiteContext(
        turn_id="t", agent_slug="ace", site="canopy-web", resource=self_host.resource(),
        scope="", expires_at=None, ceiling=list(ceiling), _token=raw)


# --- the whole round trip ---------------------------------------------------------------


def test_a_member_on_the_inbox_page_gets_an_agent_that_reads_as_them(world, gateway_to_self):
    inner, _app = gateway_to_self
    visitor = world["visitor"]

    # 1 + 2: the widget mints; canopy issues and redeems its own grant.
    r = _mint(visitor, "agent.inbox")
    assert r.status_code == 200, r.content
    assert r.json()["host_grant"] is True

    grant = HostGrant.objects.get(app=world["app"])
    assert grant.user == visitor and grant.contact is None and grant.subject == str(visitor.pk)
    assert grant.scope == "items:read"
    assert grant.dpop_jkt == client_identity.dpop_jkt(), "bound to canopy's DPoP key"
    from canopy_sdk.django.models import DelegatedToken as IssuedByHost

    issued = IssuedByHost.objects.get()
    raw = decrypt_secret(grant.access_token_enc)
    assert issued.token_checksum == contract.token_checksum(raw), "the host issued what canopy stored"
    assert issued.subject == str(visitor.pk) and issued.client_id == client_identity.client_id()
    assert issued.actor == client_identity.client_id()

    # 3: the agent's call, through canopy's mounted MCP, into canopy's own MCP.
    turn = _visitor_turn(world)
    with _as_visitor(turn):
        out = _in_lifespan(inner, lambda: mcp.call_tool("site_call", {"tool": "list_items",
                                                                      "arguments": {}}))
    body = out.structured_content
    assert body["is_error"] is False, body
    text = " ".join(block.get("text", "") for block in body["content"])
    assert "visible to gillian" in text
    assert "NOT visible" not in text, "the tool ran with the visitor's own ACL"

    # The tool ran AS the visitor: canopy's audit says who.
    row = MCPAuditLog.objects.filter(tool="list_items").get()
    assert row.user_id == visitor.pk and row.ok


def test_the_site_lists_only_the_tools_the_grant_allows(world, gateway_to_self):
    inner, _app = gateway_to_self
    _mint(world["visitor"], "agent.inbox")
    turn = _visitor_turn(world)
    with _as_visitor(turn):
        out = _in_lifespan(inner, lambda: mcp.call_tool("site_tools", {}))
    names = [t["name"] for t in out.structured_content["tools"]]
    assert names == ["list_items"]


# --- scope limits at canopy's own MCP -------------------------------------------------------


def test_a_delegated_token_reaches_only_its_scopes_tools(world, gateway_to_self):
    inner, _app = gateway_to_self
    assert _mint(world["visitor"], "agent.inbox").json()["host_grant"] is True
    raw = decrypt_secret(HostGrant.objects.get().access_token_enc)
    assert HostGrant.objects.get().scope == "items:read"

    # Ask the HOST with a context whose ceiling lets anything through, to prove
    # the host enforces the grant's scope on its own -- which is what the gateway
    # relies on, since it adds no narrowing of its own without a ceiling.
    wide = _ctx(raw)
    listed = _in_lifespan(inner, lambda: host_gateway.list_tools(wide))
    assert [t["name"] for t in listed] == ["list_items"]

    # A read tool another page's scope unlocks (`skills:read`) is out of reach...
    out = _in_lifespan(inner, lambda: host_gateway.call_tool(
        wide, "skill_history", {"slug": world["agent"].slug}))
    assert out["is_error"] is True
    assert "not within this grant" in out["content"][0]["text"]
    # ...and so is a write on the very rows the grant can read.
    mine = world["mine"]
    out = _in_lifespan(inner, lambda: host_gateway.call_tool(
        wide, "dismiss_item", {"item_id": str(mine.uuid)}))
    assert out["is_error"] is True, "a write tool is never reachable with a read grant"
    mine.refresh_from_db()
    assert mine.decided_at is None, "the item was not dismissed"


def test_a_bound_token_sent_as_a_plain_bearer_is_refused(world, mcp_app):
    inner, app = mcp_app
    _mint(world["visitor"], "agent.inbox")
    raw = decrypt_secret(HostGrant.objects.get().access_token_enc)

    async def post(headers):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as http:
            r = await http.post("/api/mcp/", headers={"Accept": "application/json, text/event-stream",
                                                     **headers},
                                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            return r.status_code

    assert _in_lifespan(inner, lambda: post({"Authorization": f"Bearer {raw}"})) == 401
    # And a DPoP header with a proof from a key the token is not bound to.
    stranger = generate_private_key()
    from canopy_sdk.consumer import dpop_proof

    proof = dpop_proof(stranger, "POST", self_host.resource(), access_token=raw)
    assert _in_lifespan(inner, lambda: post({"Authorization": f"DPoP {raw}", "DPoP": proof})) == 401


def test_a_deactivated_visitor_s_token_stops_working(world, gateway_to_self):
    inner, _app = gateway_to_self
    _mint(world["visitor"], "agent.inbox")
    raw = decrypt_secret(HostGrant.objects.get().access_token_enc)
    User.objects.filter(pk=world["visitor"].pk).update(is_active=False)
    with pytest.raises(host_gateway.GatewayRefusal):
        _in_lifespan(inner, lambda: host_gateway.list_tools(_ctx(raw)))


# --- refusals at issue time -----------------------------------------------------------------


def test_an_unregistered_page_gets_no_grant_and_the_mint_still_works(world):
    r = _mint(world["visitor"], "settings")
    assert r.status_code == 200 and r.json()["token"]
    assert r.json()["host_grant"] is False
    assert not HostGrant.objects.exists()
    assert _mint(world["visitor"], "").json()["host_grant"] is False


def test_a_site_that_does_not_name_this_deployment_gets_no_self_grant(world):
    app = world["app"]
    app.host_issuer = "https://elsewhere.test"
    app.save()
    assert _mint(world["visitor"], "agent.inbox").json()["host_grant"] is False
    assert not HostGrant.objects.exists()


@override_settings(CANOPY_HOST_SIGNING_KEY="", CANOPY_OAUTH_EPHEMERAL_KEYS=False,
                   CANOPY_OAUTH_CLIENT_KEY="", CANOPY_OAUTH_DPOP_KEY="")
def test_everything_is_off_until_configured(world, mcp_app):
    assert not self_host.configured()
    r = _mint(world["visitor"], "agent.inbox")
    assert r.status_code == 200 and r.json()["host_grant"] is False
    c = Client()
    assert c.get("/oauth/host/jwks.json").status_code == 503
    # The discovery document is still served — a PERSON's MCP login
    # (apps/tokens/mcp_oauth.py) needs no host keys — but it offers no
    # jwt-bearer grant until this deployment is a host.
    meta = c.get("/.well-known/oauth-authorization-server")
    assert meta.status_code == 200
    assert contract.JWT_BEARER_GRANT not in meta.json()["grant_types_supported"]
    r = c.post("/oauth/token", {"grant_type": contract.JWT_BEARER_GRANT})
    assert r.status_code == 400 and r.json()["error"] == "unsupported_grant_type"

    inner, app = mcp_app

    async def post():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as http:
            return (await http.post("/api/mcp/", headers={"Authorization": "DPoP x", "DPoP": "y"},
                                    json={})).status_code

    assert _in_lifespan(inner, post) == 401, "a DPoP request to a canopy that is not a host is a 401, not a 500"


def test_a_configured_pem_signing_key_is_the_one_published(world):
    key = generate_private_key("ES256")
    with override_settings(CANOPY_HOST_SIGNING_KEY=private_pem(key)):
        from canopy_sdk.keys import public_jwk

        published = Client().get("/oauth/host/jwks.json").json()["keys"]
        assert [k["kid"] for k in published] == [public_jwk(key)["kid"]]
        assert _mint(world["visitor"], "agent.inbox").json()["host_grant"] is True


# --- the public surface ----------------------------------------------------------------------


def test_the_discovery_documents_and_token_endpoint_are_public(world):
    c = Client()
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == BASE and meta["token_endpoint"] == f"{BASE}/oauth/token"
    assert contract.JWT_BEARER_GRANT in meta["grant_types_supported"]
    # The host grant's read-only scopes, plus the person-login scope
    # (apps/tokens/mcp_oauth.py) that shares this issuer.
    assert set(meta["scopes_supported"]) == set(self_host.SCOPE_TOOLS) | {"canopy"}
    # The RFC 8414 path-inserted form, as a deployment under a prefix is asked.
    assert c.get("/.well-known/oauth-authorization-server/canopy").json()["issuer"] == BASE
    prm = c.get("/.well-known/oauth-protected-resource/api/mcp").json()
    assert prm["resource"] == f"{BASE}/api/mcp/" and prm["authorization_servers"] == [BASE]

    # Anything but the jwt-bearer grant is refused; a jwt-bearer grant with no
    # client authentication is refused as the SDK refuses it.
    assert c.post("/oauth/token", {"grant_type": "client_credentials"}).json()["error"] == \
        "unsupported_grant_type"
    r = c.post("/oauth/token", {"grant_type": contract.JWT_BEARER_GRANT, "client_id": "nope"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"


def test_the_widget_names_exactly_the_pages_the_server_registers():
    """A key the server does not know is silently no grant, so the two lists
    must not drift: `frontend/src/widget/grantPage.ts` names every page here."""
    import pathlib
    import re

    source = (pathlib.Path(__file__).resolve().parent.parent
              / "frontend/src/widget/grantPage.ts").read_text()
    named = set(re.findall(r"/, '([a-z_.]+)'\]", source))
    assert named == set(self_host.PAGE_SCOPES)


def test_every_page_scope_is_a_scope_the_host_offers_and_is_read_only():
    for page, scopes in self_host.PAGE_SCOPES.items():
        assert scopes and set(scopes) <= set(self_host.SCOPE_TOOLS), page
    tools = {t for ts in self_host.SCOPE_TOOLS.values() for t in ts}
    assert not {t for t in tools if t.startswith(("clear_", "dismiss_", "create_", "delete_",
                                                  "update_", "run_", "purge_"))}
