"""MCP Apps at the gateway: negotiation, the UI tool index, and visibility.

Spec 2026-10-08 §1-2. Driven through the MOUNTED canopy MCP server with a real
caller token, against a real FastMCP host (`tests/mcp_apps_host.py`) reached
in-process, exactly as `test_site_gateway` drives the gateway.
"""
from __future__ import annotations

import contextlib
from datetime import timedelta

import httpx2
import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.utils import timezone
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.common.encryption import encrypt_secret
from apps.contacts.models import Contact
from apps.harness import caller_tokens
from apps.harness.models import Turn
from apps.mcp.auth import CanopyPATVerifier
from apps.mcp.models import MCPAuditLog
from apps.mcp.server import mcp
from apps.tokens import client_identity, host_gateway, mcp_apps
from apps.tokens.models import AppCredential, AppCredentialAgent, HostGrant
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.mcp_apps_host import VIEW_URI, FakeLabs

pytestmark = pytest.mark.django_db

RESOURCE = "https://labs.test/mcp/"
TOKEN = "grant-visitor"
IFACE = {"capabilities": {
    "connect": {"callers": ["contact"], "sites": ["connect-labs"],
                "tools": ["mcp__*canopy-web__who_is_asking"]},
}}


@pytest.fixture()
def labs(monkeypatch):
    fake = FakeLabs(tokens={TOKEN})
    monkeypatch.setattr(host_gateway, "_transport_override", httpx2.ASGITransport(app=fake.app))
    return fake


@pytest.fixture()
def w():
    owner = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner,
                                 interface=parse(IFACE))
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    app.host_issuer = "https://labs.test"
    app.host_mcp_resource = RESOURCE
    app.allowed_frame_origins = ["https://labs.test"]
    app.save()
    AppCredentialAgent.objects.create(app=app, agent=agent)
    contact = Contact.objects.create(workspace=ws, app=app, external_id="u-42",
                                     display_name="Gillian", source=Contact.SOURCE_EMBED)
    session = Session.objects.create(workspace=ws, agent=agent, contact=contact,
                                     metadata={"embed_app": "connect-labs"})
    turn = Turn.objects.create(chat_session=session, prompt="coach them",
                               initiator_contact=contact, initiator_kind="contact",
                               capability="connect")
    HostGrant.objects.create(
        app=app, subject="u-42", contact=contact, access_token_enc=encrypt_secret(TOKEN),
        scope="workflow:act", resource=RESOURCE, dpop_jkt=client_identity.dpop_jkt(),
        expires_at=timezone.now() + timedelta(minutes=10))
    return {"app": app, "turn": turn, "session": session}


@contextlib.contextmanager
def visitor(w):
    access = async_to_sync(CanopyPATVerifier().verify_token)(caller_tokens.mint(w["turn"]))
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def call(labs, name, args):
    async def main():
        async with labs.lifespan():
            try:
                return True, await mcp.call_tool(name, args)
            except Exception as exc:  # noqa: BLE001
                return False, exc
    ok, out = async_to_sync(main)()
    if not ok:
        raise out
    return out


# --- §1 negotiation -------------------------------------------------------------------


def test_the_gateway_advertises_the_ui_extension_and_the_host_sees_it(w, labs):
    with visitor(w):
        out = call(labs, "site_call", {"tool": "workflow_run_action",
                                       "arguments": {"run_id": 1, "action": "coach"}})
    assert labs.ui_negotiated == [True]
    # Labs item 6: on a UI-negotiated connection the model's preview has no token.
    assert "confirm-" not in str(out.structured_content)
    assert "confirm" not in out.structured_content["structured"]


# --- §2 the index and visibility -------------------------------------------------------


def test_site_tools_hides_app_only_tools_and_keeps_model_ones(w, labs):
    with visitor(w):
        out = call(labs, "site_tools", {}).structured_content
    names = {t["name"] for t in out["tools"]}
    assert "workflow_action_preview_view" not in names
    assert {"workflow_run_action", "model_only_report", "marketplace_orgs_get"} <= names


def test_listing_fills_the_ui_tool_index_from_meta_ui_only(w, labs):
    with visitor(w):
        call(labs, "site_tools", {})
    w["app"].refresh_from_db()
    tools = w["app"].mcp_apps_index["tools"]
    assert tools["workflow_run_action"]["resource_uri"] == VIEW_URI
    assert tools["workflow_run_action"]["visibility"] == ["model", "app"]
    assert tools["workflow_action_preview_view"]["visibility"] == ["app"]
    # The deprecated flat `_meta["ui/resourceUri"]` is not read.
    assert "legacy_flat_meta" not in tools
    # A tool with no `_meta.ui` has no entry: default visibility, no View.
    assert "marketplace_orgs_get" not in tools


@pytest.mark.parametrize("fresh_index", [True, False])
def test_site_call_refuses_an_app_only_tool_and_audits_it(w, labs, fresh_index):
    if fresh_index:
        with visitor(w):
            call(labs, "site_tools", {})
    labs.calls.clear()
    with visitor(w), pytest.raises(Exception, match="only for the page's own view"):
        call(labs, "site_call", {"tool": "workflow_action_preview_view",
                                 "arguments": {"run_id": 1, "action": "coach"}})
    assert "workflow_action_preview_view" not in labs.tools_called()
    row = MCPAuditLog.objects.filter(tool="site_call").latest("id")
    assert row.ok is False and row.error == "not_allowed"


def test_site_call_still_reaches_a_model_visible_tool(w, labs):
    with visitor(w):
        out = call(labs, "site_call", {"tool": "model_only_report", "arguments": {"run_id": 3}})
    assert out.structured_content["is_error"] is False
    assert labs.tools_called()[-1] == "model_only_report"


def test_the_index_is_a_union_and_forgets_tools_the_host_stopped_marking(w):
    app = w["app"]
    mcp_apps.record_tools(app.pk, [("a", {"ui": {"resourceUri": "ui://x/a"}}),
                                   ("b", {"ui": {"visibility": ["app"]}})])
    mcp_apps.record_tools(app.pk, [("a", {})])  # the host took a's View away
    app.refresh_from_db()
    assert set(app.mcp_apps_index["tools"]) == {"b"}


# --- metadata parsing --------------------------------------------------------------------


@pytest.mark.parametrize("meta,model,app_", [
    ({}, True, True),
    ({"ui": {}}, True, True),
    ({"ui": {"visibility": ["app"]}}, False, True),
    ({"ui": {"visibility": ["model"]}}, True, False),
    ({"ui": {"visibility": "app"}}, True, True),          # malformed -> the default
    ({"ui": {"visibility": ["bogus"]}}, True, True),     # nothing readable -> the default
    ({"ui": {"visibility": []}}, False, False),          # explicitly nobody
])
def test_visibility_defaults_as_the_spec_says(meta, model, app_):
    assert mcp_apps.model_visible(meta) is model
    assert mcp_apps.app_visible(meta) is app_


def test_only_ui_scheme_resource_uris_count():
    assert mcp_apps.ui_meta({"ui": {"resourceUri": "https://evil/x"}})["resource_uri"] == ""
    assert mcp_apps.ui_meta({"ui/resourceUri": "ui://x"}) == {}
