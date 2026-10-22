"""canopy-web as the gateway to a host's MCP, the world its suites share: a real
FastMCP server standing in for the host (checking every DPoP proof the way the
contract tells a host to), and canopy's side of one visitor's conversation on it.

Plain builders, not plugin fixtures: each suite wraps them in its own `host` / `w`
fixtures (`tests/test_site_gateway.py`, `tests/test_execution_statement.py`).
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
from datetime import timedelta

import httpx2
import jwt
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.utils import timezone
from fastmcp import FastMCP
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.common.encryption import encrypt_secret
from apps.contacts.models import Contact
from apps.harness import caller_tokens
from apps.harness.models import Turn
from apps.host_grants import client_identity, host_gateway
from apps.mcp.auth import CanopyPATVerifier
from apps.mcp.server import mcp
from apps.tokens.models import AppCredential, AppCredentialAgent, HostGrant
from apps.workspaces.models import Workspace, WorkspaceMembership

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


def host_mcp(monkeypatch):
    h = HostMCP()
    monkeypatch.setattr(host_gateway, "_transport_override", httpx2.ASGITransport(app=h.app))
    return h


# --- canopy's side ----------------------------------------------------------------


def world():
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


def grant(w, **over):
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


def call(host, name, args):
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


def visitor(w):
    return as_token(caller_tokens.mint(w["turn"]))
