"""The REST API served as MCP tools (`apps/mcp/api_tools.py`).

Driven through the MOUNTED server (`mcp.call_tool`), so a tool here is the real
route answering through Django's real middleware — the only way to know the
caller, the tenant and the ACL are the API's own.
"""
from __future__ import annotations

import contextlib
import json

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.models import Agent
from apps.api.api import api
from apps.common.views_debug import is_machine
from apps.mcp import api_tools
from apps.mcp.models import MCPAuditLog
from apps.mcp.server import mcp
from apps.tokens.middleware import BearerTokenAuthMiddleware
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()
M = WorkspaceMembership


@contextlib.contextmanager
def as_claims(**claims):
    access = AccessToken(token="t", client_id="c", scopes=["canopy:user"], claims=claims)
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def as_user(user):
    return as_claims(sub=str(user.pk), user_id=user.pk, auth_method="pat")


def _call(name, args=None):
    result = async_to_sync(mcp.call_tool)(name, args or {})
    return result.structured_content


def _tools():
    return {t.name: t for t in async_to_sync(mcp.list_tools)()}


def _schema():
    return json.loads(json.dumps(api.get_openapi_schema(), default=str))


def _operation_ids():
    return {op["operationId"] for item in _schema()["paths"].values() for op in item.values()}


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    cache.clear()


@pytest.fixture()
def tenancy(db):
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    other = User.objects.create_user("other", "other@dimagi.com", "pw")
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    wsvc.ensure_member(connect, jj, M.OWNER)
    wsvc.ensure_member(dimagi, jj, M.OWNER)
    secret = Workspace.objects.create(slug="secret", display_name="Secret", created_by=other)
    wsvc.ensure_member(secret, other, M.OWNER)
    Agent.objects.create(slug="hal", name="Hal", workspace=connect, owner=jj)
    Agent.objects.create(slug="eva", name="Eva", workspace=dimagi, owner=jj)
    Agent.objects.create(slug="spy", name="Spy", workspace=secret, owner=other)
    return {"jj": jj, "other": other}


# -- the surface -----------------------------------------------------------

def test_every_route_is_a_tool_or_excluded_for_a_reason(db):
    tools = _tools()
    schema = _schema()
    for path, item in schema["paths"].items():
        for method, op in item.items():
            reason = api_tools.excluded_reason(path, method, op)
            name = op["operationId"]
            if reason is None:
                assert name in tools, f"{method.upper()} {path} is not on MCP"
                assert isinstance(tools[name], api_tools.CanopyAPITool)
            else:
                assert isinstance(reason, str) and reason


def test_exclusions_name_routes_that_exist():
    """An exclusion for a route that was renamed or deleted is dead weight that
    would silently stop applying — fail instead."""
    ids = _operation_ids()
    assert set(api_tools.EXCLUDED) <= ids
    paths = _schema()["paths"]
    for prefix in api_tools.EXCLUDED_PREFIXES:
        assert any(p.startswith(prefix) for p in paths), prefix


def test_no_generated_tool_collides_with_a_hand_written_one(db):
    """FastMCP resolves hand-written tools first, so a route sharing a
    hand-written tool's name would be silently unreachable. A hand-written tool
    exists only for what is not a route; if one duplicates a route, delete it."""
    generated = api_tools.tool_spec(_schema())
    names = [op["operationId"] for item in generated["paths"].values() for op in item.values()]
    assert len(names) == len(set(names))
    tools = _tools()
    for name in names:
        assert isinstance(tools[name], api_tools.CanopyAPITool), name


def test_configuration_routes_are_on_mcp(db):
    """The point of the surface: configuring agents and workspaces."""
    tools = _tools()
    for name in ("upsert_agent", "replace_agent_runners",
                 "replace_agent_runner_rules", "set_turn_mode", "set_slack_enabled",
                 "connect_app", "set_push_config",
                 "set_history", "pause_runner", "set_runner_credential"):
        assert name in tools, name


def test_tools_carry_the_route_and_a_workspace_argument(db):
    tool = _tools()["set_turn_mode"]
    assert tool.description.startswith("`PATCH /api/agents/{slug}/turn-mode`")
    assert "workspace" in tool.parameters["properties"]
    assert tool.annotations.read_only_hint is False
    assert _tools()["list_agents"].annotations.read_only_hint is True
    assert _tools()["delete_agent"].annotations.destructive_hint is True


def test_a_route_that_names_its_workspace_keeps_its_own_argument(db):
    """`/api/slack-config/{workspace}` already takes one; adding a second would
    shadow the path parameter."""
    tool = _tools()["get_config"]
    assert tool.parameters["properties"]["workspace"].get("description") != (
        "Workspace slug to act in. Omit to act across your workspaces, as the flat route does.")


# -- as the caller ---------------------------------------------------------

def test_a_read_runs_as_the_caller(tenancy):
    with as_user(tenancy["jj"]):
        rows = _call("list_workspaces")["result"]
    assert {w["slug"] for w in rows} == {"connect", "dimagi"}
    with as_user(tenancy["other"]):
        rows = _call("list_workspaces")["result"]
    assert {w["slug"] for w in rows} == {"secret"}


def test_workspace_argument_selects_the_tenant(tenancy):
    with as_user(tenancy["jj"]):
        agents = _call("list_agents", {"workspace": "dimagi"})
    slugs = {a["slug"] for a in (agents.get("result") or agents.get("items") or [])}
    assert "eva" in slugs and "hal" not in slugs


def test_a_workspace_you_are_not_in_is_not_found(tenancy):
    with as_user(tenancy["jj"]), pytest.raises(ToolError, match="404"):
        _call("list_agents", {"workspace": "secret"})


def test_another_tenants_agent_is_not_reachable(tenancy):
    with as_user(tenancy["jj"]), pytest.raises(ToolError, match="404"):
        _call("get_agent", {"slug": "spy"})


def test_a_write_changes_state_and_is_audited(tenancy):
    with as_user(tenancy["jj"]):
        out = _call("set_turn_mode", {"slug": "hal", "turn_mode": "auto"})
    assert out["turn_mode"] == "auto"
    assert Agent.objects.get(slug="hal").turn_mode == "auto"
    row = MCPAuditLog.objects.filter(tool="set_turn_mode").latest("id")
    assert row.ok is True and row.user_id == tenancy["jj"].pk


def test_a_refused_write_is_a_tool_error_and_audited(tenancy):
    with as_user(tenancy["other"]), pytest.raises(ToolError):
        _call("set_turn_mode", {"slug": "hal", "turn_mode": "auto"})
    assert Agent.objects.get(slug="hal").turn_mode != "auto"
    assert MCPAuditLog.objects.filter(tool="set_turn_mode", ok=False).exists()


def test_writes_are_rate_limited(tenancy, settings):
    settings.MCP_WRITE_LIMIT = 1
    with as_user(tenancy["jj"]):
        _call("set_turn_mode", {"slug": "hal", "turn_mode": "auto"})
        with pytest.raises(ToolError, match="rate limit"):
            _call("set_turn_mode", {"slug": "hal", "turn_mode": "manual"})


def test_a_confined_session_does_not_see_the_api(tenancy):
    """A caller token reaches only what its capability lists (TurnScopeMiddleware)."""
    with as_claims(sub="turn:1", user_id=None, auth_method="caller_token",
                   turn_id="1", turn_ids=["1"], tool_globs=[]), \
            pytest.raises(ToolError, match="not part of what this caller"):
        _call("list_workspaces")


def test_a_token_with_no_canopy_user_is_refused(tenancy):
    with as_claims(sub="x", auth_method="pat"), pytest.raises(ToolError, match="canopy user"):
        _call("list_workspaces")


def test_unauthenticated_is_refused(db):
    with pytest.raises(ToolError):
        _call("list_workspaces")


# -- the handoff into Django ----------------------------------------------

def _request_with_scope(scope):
    request = RequestFactory().get("/api/me/")
    request.scope = scope
    from django.contrib.auth.models import AnonymousUser

    request.user = AnonymousUser()
    return request


def test_the_scope_principal_signs_the_request_in_as_a_machine(tenancy):
    request = _request_with_scope({
        api_tools.MCP_PRINCIPAL_SCOPE_KEY: {"user_id": tenancy["jj"].pk, "auth_method": "pat"},
    })
    BearerTokenAuthMiddleware._authenticate(request)
    assert request.user == tenancy["jj"]
    assert request.auth_method == "pat"
    assert is_machine(request) is True


def test_an_inactive_user_is_not_signed_in(tenancy):
    tenancy["jj"].is_active = False
    tenancy["jj"].save()
    request = _request_with_scope({
        api_tools.MCP_PRINCIPAL_SCOPE_KEY: {"user_id": tenancy["jj"].pk, "auth_method": "pat"},
    })
    BearerTokenAuthMiddleware._authenticate(request)
    assert not request.user.is_authenticated


def test_a_request_without_the_scope_key_is_untouched(tenancy):
    request = _request_with_scope({"type": "http"})
    BearerTokenAuthMiddleware._authenticate(request)
    assert not request.user.is_authenticated
    assert not is_machine(request)


def test_machine_only_refusals_are_not_offered(db):
    tools = _tools()
    for name in ("transfer_owner", "grant_admin", "revoke_admin", "link_canopy_user",
                 "set_runner_flags", "publish_interface", "set_agent_credentials",
                 "set_agent_vault", "set_shared_vault", "create_invite", "set_member_role",
                 "remove_member", "create_token"):
        assert name not in tools


def test_a_script_prefix_is_not_part_of_a_tool_path():
    """Labs serves canopy under /canopy, and Ninja writes that into every path.
    The tools must still see `/api/…`, or exclusions and the workspace rewrite
    (both written against Django's routed path) silently stop applying."""
    from django.urls import get_script_prefix, set_script_prefix

    before = get_script_prefix()
    set_script_prefix("/canopy/")
    try:
        paths = api_tools.api_schema()["paths"]
    finally:
        set_script_prefix(before)
    assert paths and all(p.startswith("/api/") for p in paths)
    spec = api_tools.tool_spec({"paths": paths})
    assert not any(p.startswith(("/api/contact/", "/api/embed/")) for p in spec["paths"])
