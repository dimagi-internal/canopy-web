"""The REST API, served as MCP tools — one tool per route, generated.

The MCP surface does not re-implement anything. Every tool here is a route of
the single `NinjaAPI` (`apps/api/api.py`): its name is the route's operationId,
its parameters are the route's path/query/body schema, its description is the
route's docstring, and calling it is an HTTP request to that route. So anything
the web app can do — the web app calls the same REST API — an MCP client can do,
through the same middleware, tenancy, ACL, validation and errors, and a new
route is on MCP the moment it exists.

**In-process, and as the caller.** The request never leaves the process: the
httpx client's transport is Django's own ASGI handler, called directly (no
socket, no loopback URL to configure). Who is asking travels in the ASGI SCOPE
(`MCP_PRINCIPAL_SCOPE_KEY`), which `BearerTokenAuthMiddleware` resolves to
`request.user` — a key no network request can carry, because only this
transport builds the scope. That is the difference from the OpenAPI-derived
server deleted in May 2026, which looped back over HTTP to localhost with ONE
shared bearer, so every tool ran as the same identity.

**Exposure is the default.** A tool is exactly as powerful as the caller's
token already is against REST, so leaving a route out is never a security
decision — the list below is of routes whose CALLER is not a person (a runner,
a browser tab, an embedding host, an anonymous reader), where a tool would only
be noise. `tests/test_mcp_api_tools.py` fails when an entry names a route that
no longer exists, so the list cannot rot.

**Workspace.** Every generated tool takes an optional `workspace`: the call is
sent to `/api/w/{workspace}/…`, the canonical tenant URL, where
`WorkspaceResolveMiddleware` checks membership. Omitted, the flat route resolves
to the caller's default workspace — the same compat shim a PAT caller gets.
"""
from __future__ import annotations

import contextvars
import json
import logging
from typing import Any

import httpx2
from asgiref.sync import sync_to_async
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.providers.openapi import OpenAPIProvider
from fastmcp.server.providers.openapi.components import OpenAPITool
from mcp.types import ToolAnnotations

from .audit import write_audit
from .rate_limit import RateLimitError, check_write_limit

logger = logging.getLogger(__name__)

#: The ASGI scope key carrying the MCP caller into Django. Read by
#: `apps.tokens.middleware.BearerTokenAuthMiddleware`.
MCP_PRINCIPAL_SCOPE_KEY = "canopy.mcp_principal"

WORKSPACE_ARG = "workspace"

# -- what is not a tool, and why -------------------------------------------

_RUNNER = "runner protocol: a runner calls this on its own tick, not a person"
_BROWSER = "a browser tab's own plumbing (page state, live viewer, push subscription)"
_HOST = "an embedding host's or a contact's surface — a different principal"
_ANON = "anonymous public read; nothing to do as a signed-in person"
_UI_ONLY = "refused to any token by design (`is_machine`): canopy's web app only"
_BYTES = "returns raw bytes, which a tool result cannot carry"

#: Path prefixes whose every route belongs to another principal.
EXCLUDED_PREFIXES: dict[str, str] = {
    "/api/contact/": _HOST,
    "/api/embed/": _HOST,
    "/api/a2a/": "A2A discovery, served to other agents over A2A itself",
    "/api/share/": _ANON,
}

#: operationId → why it is not a tool.
EXCLUDED: dict[str, str] = {
    "_auth_smoke": "internal smoke route",
    "health": "health check",
    "contact_token": _HOST,
    "public_stats": _ANON,
    "google_callback": "OAuth redirect target; Google's browser redirect calls it",
    "create_token": "minting credentials from a credential; mint a PAT in the web app",
    # Runner protocol.
    "pair_runner": _RUNNER,
    "get_runner_credential": _RUNNER,
    "turn_github_token": _RUNNER,
    "runner_github_readiness": _RUNNER,
    "claim_runner_mint": _RUNNER,
    "post_runner_mint_url": _RUNNER,
    "post_runner_mint_result": _RUNNER,
    "runner_heartbeat": _RUNNER,
    "claim_turn": _RUNNER,
    "resolve_session": _RUNNER,
    "record_session": _RUNNER,
    "report_sessions": _RUNNER,
    "list_streams": _RUNNER,
    "post_session_stream": _RUNNER,
    "list_backfills": _RUNNER,
    "list_closes": _RUNNER,
    "list_menu_answers": _RUNNER,
    "post_menu_answer_result": _RUNNER,
    "post_session_backfill": _RUNNER,
    "append_turn_events": _RUNNER,
    "append_turn_transcript": _RUNNER,
    "start_turn": _RUNNER,
    "finish_turn": _RUNNER,
    "sync_schedules": _RUNNER,
    "fire_schedule_route": _RUNNER,
    "report_drill": _RUNNER,
    "resolve_agent_credentials": _RUNNER,
    "post_bootstrap_report": _RUNNER,
    "runner_mailboxes": _RUNNER,
    "report_watch": _RUNNER,
    "gmail_push": "Google Pub/Sub push webhook",
    "list_for_key": "read with a chat's session key, from inside that session",
    "value_for_key": "read with a chat's session key, from inside that session",
    # Browser plumbing.
    "attach_session": _BROWSER,
    "detach_session": _BROWSER,
    "canopy_sessions_declare_page_state": _BROWSER,
    "canopy_sessions_declare_page_actions": _BROWSER,
    "canopy_sessions_resolve_page_action": _BROWSER,
    "declare_run_input": _BROWSER,
    "vapid_public_key": _BROWSER,
    "subscribe": _BROWSER,
    "unsubscribe": _BROWSER,
    "attachment_content": _BYTES,
    # Web app only, by design.
    "transfer_owner": _UI_ONLY,
    "grant_admin": _UI_ONLY,
    "revoke_admin": _UI_ONLY,
    # Deprecated.
    "set_runner_preference": "deprecated; superseded by replace_agent_runners",
}

#: Hand-written tools that share a route's name. The hand-written tool wins
#: (FastMCP resolves static tools ahead of providers), and the generated twin is
#: not listed, so a client never sees two tools with one name. These predate the
#: generated surface and are wired into the host-grant scopes and page contract
#: by name; retiring them onto their routes is its own change.
SHADOWED_BY_HAND_WRITTEN: frozenset[str] = frozenset({
    "list_insights", "clear_insights", "list_items",
    "list_schedules", "create_schedule", "update_schedule", "delete_schedule",
})


def excluded_reason(path: str, method: str, operation: dict) -> str | None:
    """Why this operation is not a tool, or None when it is one."""
    op_id = operation.get("operationId", "")
    if op_id in EXCLUDED:
        return EXCLUDED[op_id]
    if op_id in SHADOWED_BY_HAND_WRITTEN:
        return "a hand-written tool of the same name serves it"
    for prefix, reason in EXCLUDED_PREFIXES.items():
        if path.startswith(prefix):
            return reason
    content = (operation.get("requestBody") or {}).get("content") or {}
    if content and "application/json" not in content:
        return "file upload (multipart); a tool call carries JSON"
    return None


def tool_spec(schema: dict) -> dict:
    """The OpenAPI document restricted to the operations that become tools."""
    paths: dict[str, dict] = {}
    for path, item in schema.get("paths", {}).items():
        kept = {
            method: op for method, op in item.items()
            if isinstance(op, dict) and excluded_reason(path, method, op) is None
        }
        if kept:
            paths[path] = kept
    return {**schema, "paths": paths}


# -- the in-process transport ---------------------------------------------

#: The caller + workspace of the tool call in flight, read by `_django_app`.
#: Same task as the tool's `run`, so the value is exactly this call's.
_current_call: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "canopy_mcp_current_call", default=None,
)

_django_handler = None


async def _django_app(scope, receive, send):
    """Django's ASGI handler, with the tool call's caller in the scope."""
    global _django_handler
    if _django_handler is None:
        from django.core.asgi import get_asgi_application

        _django_handler = get_asgi_application()
    call = _current_call.get()
    if scope["type"] == "http" and call is not None:
        scope = dict(scope)
        scope[MCP_PRINCIPAL_SCOPE_KEY] = call["principal"]
        ws = call.get("workspace")
        if ws and scope["path"].startswith("/api/"):
            path = f"/api/w/{ws}/{scope['path'][len('/api/'):]}"
            scope["path"] = path
            scope["raw_path"] = path.encode()
    await _django_handler(scope, receive, send)


def _internal_host() -> str:
    """A host Django accepts (`ALLOWED_HOSTS`) — the request never touches it."""
    from django.conf import settings

    for host in getattr(settings, "ALLOWED_HOSTS", []) or []:
        host = host.lstrip(".")
        if host and host != "*" and "*" not in host:
            return host
    return "localhost"


def _client() -> httpx2.AsyncClient:
    # https so a deployment with SECURE_SSL_REDIRECT does not answer 301.
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=_django_app),
        base_url=f"https://{_internal_host()}",
        timeout=120.0,
    )


# -- the tool --------------------------------------------------------------

def _principal() -> dict:
    """The caller, from the MCP access token — or a ToolError saying why not."""
    token = get_access_token()
    claims = (token.claims or {}) if token is not None else {}
    user_id = claims.get("user_id")
    if user_id is None:
        raise ToolError("This tool acts as a canopy user; sign in with a personal access token.")
    return {"user_id": int(user_id), "auth_method": claims.get("auth_method") or "pat"}


class CanopyAPITool(OpenAPITool):
    """An OpenAPI tool that runs as the MCP caller, audited and rate-limited
    like every canopy MCP write."""

    adds_workspace: bool = False

    async def run(self, arguments: dict[str, Any]):
        args = dict(arguments)
        workspace = args.pop(WORKSPACE_ARG, None) if self.adds_workspace else None
        principal = _principal()
        method = self._route.method.upper()
        summary = f"{method} {self._route.path} {sorted(args)}"
        if workspace:
            summary = f"[{workspace}] {summary}"
        if method != "GET":
            try:
                await sync_to_async(check_write_limit, thread_sensitive=True)(principal["user_id"])
            except RateLimitError as exc:
                await write_audit(user_id=principal["user_id"], tool=self.name,
                                  args_summary=summary, ok=False, error=str(exc))
                raise ToolError(str(exc)) from exc
        token = _current_call.set({"principal": principal, "workspace": workspace})
        try:
            result = await super().run(args)
        except Exception as exc:
            await write_audit(user_id=principal["user_id"], tool=self.name,
                              args_summary=summary, ok=False, error=str(exc))
            raise ToolError(str(exc)) from exc
        finally:
            _current_call.reset(token)
        await write_audit(user_id=principal["user_id"], tool=self.name,
                          args_summary=summary, ok=True)
        return result


def _annotations(method: str) -> ToolAnnotations:
    method = method.upper()
    return ToolAnnotations(
        read_only_hint=method == "GET",
        destructive_hint=method == "DELETE",
        idempotent_hint=method in ("GET", "PUT", "DELETE"),
        open_world_hint=False,
    )


class CanopyAPIProvider(OpenAPIProvider):
    """`OpenAPIProvider` over canopy's own schema, building `CanopyAPITool`s."""

    def _create_openapi_tool(self, route, name, tags) -> None:
        super()._create_openapi_tool(route, name, tags)
        generic = self._tools.pop(name)
        params = dict(generic.parameters or {})
        props = dict(params.get("properties") or {})
        adds_workspace = WORKSPACE_ARG not in props
        if adds_workspace:
            props[WORKSPACE_ARG] = {
                "type": "string",
                "description": (
                    "Workspace slug to act in. Omit for your default workspace."
                ),
            }
            params["properties"] = props
        output_schema = generic.output_schema
        if output_schema and output_schema.get("x-fastmcp-wrap-result"):
            # A list (or scalar) response is wrapped as {"result": …}; a client
            # unwraps it and validates the inner value against
            # `properties.result`, which the permissive schema lacks — so it
            # read a list against "object" and logged a parse error per call.
            output_schema = {**output_schema, "properties": {"result": {}}}
        description = f"`{route.method.upper()} {route.path}`\n\n{generic.description or ''}".strip()
        tool = CanopyAPITool(
            client=self._client, route=route, director=self._director,
            name=generic.name, description=description, parameters=params,
            output_schema=output_schema, tags=generic.tags,
            annotations=_annotations(route.method),
        )
        tool.adds_workspace = adds_workspace
        self._tools[tool.name] = tool


def build_provider() -> CanopyAPIProvider:
    from apps.api.api import api

    # Through JSON, exactly as `/api/openapi.json` serves it: Ninja's dict keys
    # responses by int status, which the OpenAPI parser rejects.
    spec = tool_spec(json.loads(json.dumps(api.get_openapi_schema(), default=str)))
    # Output schemas are not enforced: a route's response model already is, and
    # a mismatch between the two would fail a call the API answered correctly.
    return CanopyAPIProvider(openapi_spec=spec, client=_client(), validate_output=False)
