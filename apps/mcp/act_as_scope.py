"""An act-as-you token's MCP session sees only what its permission covers.

An act-as-you token (`apps/harness/act_as.py`) signs in as the person who approved it.
What it may reach is ONE rule, `act_as.route_refusal`, asked here for every tool that is a
REST route (its `_route`), and asked again by the in-process request itself
(`tokens.middleware._act_as_mcp_refusal`) — so MCP and REST cannot drift. A hand-written
tool (not a route) is called only when it only reads (`readOnlyHint`) and is not one
`act_as.EXCLUDED_TOOLS` names; such a call is logged here, because no route logs it.

Listing and calling are both filtered. Every other credential passes through untouched.
"""
from __future__ import annotations

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

from apps.tokens import kinds

from .audit import write_audit
from .turn_scope import read_only


def _claims() -> dict | None:
    try:
        tok = get_access_token()
    except Exception:  # noqa: BLE001 - no request context
        return None
    claims = (tok.claims or {}) if tok is not None else {}
    return claims if kinds.kind_of({"type": claims.get("auth_method")}) is kinds.KINDS[kinds.ACT_AS] else None


def refusal(tool, claims: dict) -> str | None:
    """Why this act-as session may not call `tool`, or None."""
    from apps.harness import act_as  # noqa: PLC0415 — mcp sits above harness; only an act-as session pays

    if tool is None:
        return "no such tool"
    route = getattr(tool, "_route", None)
    if route is not None:
        return act_as.route_refusal(route.method, route.path, claims.get("act_as_scope") or "",
                                    is_tool=True)
    if tool.name in act_as.EXCLUDED_TOOLS:
        return f"an act-as-you permission never reaches {tool.name} ({act_as.EXCLUDED_TOOLS[tool.name]})"
    if not read_only(tool):
        return (f"{tool.name} is not one of canopy's REST routes and changes something; an "
                "act-as-you permission covers the routes MCP serves as tools, and other tools "
                "only to read")
    return None


class ActAsScopeMiddleware(Middleware):
    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        claims = _claims()
        if claims is None:
            return tools
        return [t for t in tools if refusal(t, claims) is None]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        claims = _claims()
        if claims is None:
            return await call_next(context)
        name = context.message.name
        try:
            tool = await context.fastmcp_context.fastmcp.get_tool(name)
        except Exception:  # noqa: BLE001 — unknown is refused
            tool = None
        why = refusal(tool, claims)
        if why is not None:
            await write_audit(user_id=claims.get("user_id"), tool=name, ok=False, error=why,
                              acting_agent=claims.get("acting_agent") or "",
                              act_as_grant=claims.get("act_as_grant") or "")
            raise ToolError(why)
        if getattr(tool, "_route", None) is not None:
            return await call_next(context)  # the route tool logs itself (`api_tools`)
        try:
            result = await call_next(context)
        except Exception as exc:
            await write_audit(user_id=claims.get("user_id"), tool=name, ok=False, error=str(exc),
                              acting_agent=claims.get("acting_agent") or "",
                              act_as_grant=claims.get("act_as_grant") or "")
            raise
        await write_audit(user_id=claims.get("user_id"), tool=name, ok=True,
                          acting_agent=claims.get("acting_agent") or "",
                          act_as_grant=claims.get("act_as_grant") or "")
        return result
