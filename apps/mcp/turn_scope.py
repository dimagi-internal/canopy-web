"""A confined session's MCP calls reach canopy as `agent ∩ caller`.

Who-is-asking §7. A caller token (harness.caller_tokens) already makes the tools
run as the CALLER — their own ACL, or none for an outside contact. This
middleware adds the agent half: only the canopy tools the caller's capability
lists exist for that session. Listing and calling are both filtered, and
`who_is_asking` answers only about the session's own conversation.

Everything else — a PAT, the widget's tokens — passes through untouched.
"""
from __future__ import annotations

import fnmatch

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext


def _turn_claims() -> dict | None:
    try:
        tok = get_access_token()
    except Exception:  # noqa: BLE001 - no request context
        return None
    claims = (tok.claims or {}) if tok is not None else {}
    return claims if claims.get("auth_method") == "caller_token" else None


def _allowed(name: str, claims: dict) -> bool:
    return any(fnmatch.fnmatchcase(name, g) for g in claims.get("tool_globs") or [])


#: Tools whose `turn_id` argument must name a turn in this token's own
#: conversation.
TURN_PINNED = frozenset({"who_is_asking", "act_on_behalf_of_caller"})


class TurnScopeMiddleware(Middleware):
    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        claims = _turn_claims()
        if claims is None:
            return tools
        return [t for t in tools if _allowed(t.name, claims)]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        claims = _turn_claims()
        if claims is not None:
            name = context.message.name
            if not _allowed(name, claims):
                raise ToolError(f"{name} is not part of what this caller's session may use")
            # Every tool that takes a `turn_id` is pinned to THIS token's own
            # conversation. Without it the argument is the whole gate: a caller
            # could name someone else's turn and be told who THEY are
            # (`who_is_asking`), or be vouched for as them
            # (`act_on_behalf_of_caller`, which mints a credential). The list is
            # explicit rather than "any tool with a turn_id argument", so a new
            # tool is pinned by a person deciding to pin it.
            if name in TURN_PINNED:
                asked = str((context.message.arguments or {}).get("turn_id") or "")
                if asked not in set(claims.get("turn_ids") or []):
                    raise ToolError("turn not found")
        return await call_next(context)
