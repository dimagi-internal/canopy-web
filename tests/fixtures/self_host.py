"""canopy as its own MCP host: a workspace with an agent and the canopy-web site, canopy's
MCP mounted as config/asgi.py mounts it, and helpers to mint and use an embed grant.
Registered as a pytest plugin (root conftest.py); a suite that needs the public base URL
asks for `self_host_base`.
"""
from __future__ import annotations

import httpx2
import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings
from starlette.applications import Starlette
from starlette.routing import Mount

from apps.agents.interface import parse
from apps.agents.models import Agent, AgentTask
from apps.mcp import delegation
from apps.mcp.server import mcp
from apps.host_grants import host_gateway, self_host
from apps.tokens.models import AppCredential, AppCredentialAgent
from apps.workspaces.models import Workspace, WorkspaceMembership

BASE = "https://canopy.test"


IFACE = {"capabilities": {
    "workbench": {"callers": ["member"], "sites": ["canopy-web"],
                  "ceiling": ["mcp__*canopy-web__list_*", "mcp__*canopy-web__skill_*"],
                  "tools": ["mcp__*canopy-web__who_is_asking"]},
}}


@pytest.fixture
def self_host_base():
    cache.clear()
    with override_settings(CANOPY_PUBLIC_BASE_URL=BASE):
        yield
    cache.clear()


@pytest.fixture()
def self_host_world():
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
    # the fleet task list (`list_fleet_tasks`) must return the first and never the second.
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


def _mint(user, page, *, grant=True):
    """The panel's mint; `grant` is what it sends once the person has sent from it."""
    client = Client()
    client.force_login(user)
    return client.post(f"/api/embed/token?page={page}", data={"grant": True} if grant else {},
                       content_type="application/json")


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


def _ctx(raw, ceiling=("*",)):
    return host_gateway.SiteContext(
        turn_id="t", agent_slug="ace", site="canopy-web", resource=self_host.resource(),
        scope="", expires_at=None, ceiling=list(ceiling), _token=raw)
