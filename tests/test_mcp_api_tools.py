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


#: Routes gated `runner-protocol` / `ephemeral-credential` that ARE tools, and why
#: that is harmless. A new one must be excluded (`api_tools.EXCLUDED`) or argued here.
CREDENTIAL_ROUTES_ON_MCP: dict[str, str] = {
    **dict.fromkeys((
        "begin_passkey_registration", "begin_passkey_step_up", "finish_passkey_step_up",
    ), "a passkey ceremony: nothing it returns is a credential without the person's authenticator"),
    "create_capability_uploads": "upload URLs for the caller's own lease, the same as over REST",
    "create_runner_session": "starts a session on the caller's own runner; returns no secret",
    **dict.fromkeys((
        "box_egress", "claim_runner_pairing", "kept_conversations", "pair_runner_by_role",
        "post_delegated_checks", "post_google_mint_challenge", "post_handoff_captured",
        "post_handoff_close", "post_handoff_part", "post_handoff_restored", "post_runner_usage",
        "post_session_menu", "post_session_subagents", "post_sign_in_request",
        "post_sign_in_request_result", "register_runner_device_key", "report_runner_logins",
        "report_runner_project_result", "report_slash_commands", "request_runner_device_key",
        "turn_agent_token", "update_runner_capabilities",
    ), "runner protocol on MCP before this guard existed; a person's token reaches only its own runners"),
}


def test_credential_routes_are_excluded_or_argued(db):
    """Every route gated `kind="runner-protocol"` or `kind="ephemeral-credential"`
    hands out or acts on a credential: as a tool its answer lands in a model's
    context. So each is excluded, or named in `CREDENTIAL_ROUTES_ON_MCP` with why
    that is harmless — a new one cannot slip onto MCP by default."""
    from apps.api import route_gates

    api.get_openapi_schema()  # binds the routers
    gates = {op.operation_id or api.get_openapi_operation_id(op): route_gates.gate_of(op.view_func)
             for bound in api._get_bound_routers()
             for path_view in bound.path_operations.values() for op in path_view.operations}
    exposed = []
    for path, item in _schema()["paths"].items():
        for method, op in item.items():
            g = gates.get(op["operationId"])
            if g is None or g.kind not in ("runner-protocol", "ephemeral-credential"):
                continue
            if api_tools.excluded_reason(path, method, op) is None \
                    and op["operationId"] not in CREDENTIAL_ROUTES_ON_MCP:
                exposed.append(op["operationId"])
    assert exposed == [], f"credential routes served as MCP tools: {exposed}"
    assert set(CREDENTIAL_ROUTES_ON_MCP) <= _operation_ids()


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
                 "set_history", "pause_runner", "update_runner_credential"):
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
    from apps.mcp.api_tools import WORKSPACE_ARG_DESCRIPTION

    assert tool.parameters["properties"]["workspace"].get("description") != WORKSPACE_ARG_DESCRIPTION


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


def test_a_setting_changed_through_mcp_is_in_the_settings_audit(tenancy):
    """The same plumbing as the web UI and the CLI (`apps/events/audit.py`):
    the tool is the route, so the row names MCP and the tool."""
    from apps.events.models import SettingChange

    with as_user(tenancy["jj"]):
        _call("set_turn_mode", {"slug": "hal", "turn_mode": "auto"})
    row = SettingChange.objects.get(operation="set_turn_mode")
    assert row.via == "mcp" and row.mcp_tool == "set_turn_mode"
    assert row.actor == tenancy["jj"] and row.workspace.slug == "connect"


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


# -- a tool reaches its own route, and only that ---------------------------

@pytest.mark.parametrize("slug", ["hal/runners", "hal?x=1", "hal#x", "..", "hal\\runners"])
def test_a_path_argument_cannot_reach_another_route(tenancy, slug):
    """`get_agent(slug="hal/runners")` would otherwise be answered by
    `list_agent_runners` — a different route than the tool a confined or
    delegated caller was allowed — because the transport decodes the path."""
    with as_user(tenancy["jj"]), pytest.raises(ToolError, match="may not contain"):
        _call("get_agent", {"slug": slug})


@pytest.mark.parametrize("slug", ["hal/runners", "hal?x=1", "hal#x"])
def test_the_transport_refuses_a_smuggled_path_on_its_own(tenancy, monkeypatch, slug):
    """The second layer: with the argument check out of the way, the encoded
    path reaching `_django_app` is still refused before Django routes it."""
    monkeypatch.setattr(api_tools, "bad_path_arg", lambda value: False)
    with as_user(tenancy["jj"]), pytest.raises(ToolError, match="400"):
        _call("get_agent", {"slug": slug})


@pytest.mark.parametrize("ws", ["connect/../secret", "secret/", "Connect", "a?b", "../api"])
def test_the_workspace_argument_must_be_a_slug(tenancy, ws):
    with as_user(tenancy["jj"]), pytest.raises(ToolError, match="not a workspace slug"):
        _call("list_agents", {"workspace": ws})


def test_ordinary_path_and_workspace_arguments_still_work(tenancy):
    with as_user(tenancy["jj"]):
        assert _call("get_agent", {"slug": "hal"})["slug"] == "hal"
        assert _call("get_agent", {"slug": "eva", "workspace": "dimagi"})["slug"] == "eva"


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


def test_owner_actions_are_tools_too(db):
    """There are no web-app-only actions: inviting, roles, an agent's
    interface/credentials/admins, vaults are all MCP tools (2026-10-02).
    `create_token` is the one omission — a tool would put a raw PAT in a chat."""
    tools = _tools()
    for name in ("transfer_owner", "grant_admin", "revoke_admin", "link_canopy_user",
                 "set_runner_flags", "publish_interface", "set_agent_credentials",
                 "set_agent_vault", "create_invite", "set_member_role",
                 "remove_member"):
        assert name in tools, name
    assert "create_token" not in tools

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


def test_an_explicit_null_clears_a_field_like_rest_does(tenancy):
    """FastMCP drops `None` arguments, so `patch_task(waiting_on_email=null)`
    reached the route without the key and the field stayed set, while the same
    body sent to REST cleared it (ace T24, 2026-10-14). `NullKeepingDirector`
    sends it; an OMITTED field must still be left alone."""
    from apps.agents.models import AgentTask

    hal = Agent.objects.get(slug="hal")
    task = AgentTask.objects.create(agent=hal, ext_id="T1", title="t",
                                    next_action="keep me", waiting_on_user=tenancy["jj"])
    with as_user(tenancy["jj"]):
        out = _call("patch_task", {"slug": "hal", "ref": "T1", "waiting_on_email": None})
    assert out["waiting_on_email"] is None
    task.refresh_from_db()
    assert task.waiting_on_user_id is None
    assert task.next_action == "keep me"


def test_an_omitted_body_field_is_not_sent_as_null(tenancy):
    from apps.agents.models import AgentTask

    hal = Agent.objects.get(slug="hal")
    AgentTask.objects.create(agent=hal, ext_id="T1", title="t", waiting_on_user=tenancy["jj"])
    with as_user(tenancy["jj"]):
        out = _call("patch_task", {"slug": "hal", "ref": "T1", "next_action": "go"})
    assert out["waiting_on_email"] == "jj@dimagi.com"
    assert out["next_action"] == "go"


# -- acting as a lower role (apps/workspaces/role_cap.py) ------------------

def _send_headers(monkeypatch, **headers):
    """The MCP client's own HTTP request headers, as `get_http_headers` reads them."""
    sent = {k.replace("_", "-"): v for k, v in headers.items()}
    monkeypatch.setattr(api_tools, "get_http_headers", lambda include_all=False: dict(sent))


@pytest.mark.parametrize("cap", ["viewer", "editor"])
def test_an_acts_as_header_caps_an_mcp_call(tenancy, monkeypatch, cap):
    """`X-Canopy-Acts-As` on the MCP request caps the tool call exactly as it caps
    the same REST call: an owner acting as an editor or viewer is no agent admin."""
    _send_headers(monkeypatch, x_canopy_acts_as=cap)
    with as_user(tenancy["jj"]), pytest.raises(ToolError, match="403"):
        _call("set_turn_mode", {"slug": "hal", "turn_mode": "auto"})
    assert Agent.objects.get(slug="hal").turn_mode != "auto"


def test_an_mcp_call_without_a_cap_runs_at_the_callers_role(tenancy, monkeypatch):
    _send_headers(monkeypatch, user_agent="claude-code")
    with as_user(tenancy["jj"]):
        assert _call("set_turn_mode", {"slug": "hal", "turn_mode": "auto"})["turn_mode"] == "auto"


def test_an_acts_as_header_never_raises_a_role(tenancy, monkeypatch):
    """A cap only lowers: `other` is no member of connect, and acting as an editor
    does not make them one."""
    _send_headers(monkeypatch, x_canopy_acts_as="editor")
    with as_user(tenancy["other"]), pytest.raises(ToolError):
        _call("set_turn_mode", {"slug": "hal", "turn_mode": "auto"})
    assert Agent.objects.get(slug="hal").turn_mode != "auto"


def _capped_turn(user, cap):
    from apps.harness.models import Turn

    return Turn.objects.create(agent=Agent.objects.get(slug="hal"), prompt="ping",
                               initiator_user=user, origin_ref={"acts_as": cap})


def test_a_capped_turns_mcp_call_keeps_its_cap(tenancy, monkeypatch):
    """A session cannot shed its turn's cap by calling canopy through MCP: the turn
    named in `X-Canopy-Turn-Id`, started by this caller as a viewer, caps the call."""
    turn = _capped_turn(tenancy["jj"], "viewer")
    _send_headers(monkeypatch, x_canopy_turn_id=str(turn.pk))
    with as_user(tenancy["jj"]), pytest.raises(ToolError, match="403"):
        _call("set_turn_mode", {"slug": "hal", "turn_mode": "auto"})
    assert Agent.objects.get(slug="hal").turn_mode != "auto"


def test_a_caller_tokens_own_turn_caps_the_call(tenancy):
    """A caller token names its turn itself: no header is needed for the cap to hold.
    (A full-profile caller token is read-only now, so a write is refused before the cap
    is even asked; either way nothing changes.)"""
    turn = _capped_turn(tenancy["jj"], "viewer")
    with as_claims(sub=f"turn:{turn.pk}", user_id=tenancy["jj"].pk, auth_method="caller_token",
                   turn_id=str(turn.pk), turn_ids=[str(turn.pk)], all_tools=True,
                   tool_globs=["*"]), pytest.raises(ToolError, match="403|act-as-you permission"):
        _call("set_turn_mode", {"slug": "hal", "turn_mode": "auto"})
    assert Agent.objects.get(slug="hal").turn_mode != "auto"
