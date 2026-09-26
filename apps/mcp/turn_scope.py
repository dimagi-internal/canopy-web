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


def caller_turn_ids() -> list[str] | None:
    """The turns of THIS caller token's own conversation, or None if not one.

    Public because a tool sometimes has to answer about the conversation rather
    than about a user: a caller token's tools run as the CALLER, and an outside
    contact has no canopy account, so every user-scoped lookup correctly matches
    nothing. The token already names its conversation, which is the narrower
    question and the answerable one.
    """
    claims = _turn_claims()
    if claims is None:
        return None
    return [str(t) for t in (claims.get("turn_ids") or []) if t]


def _allowed(name: str, claims: dict) -> bool:
    return any(fnmatch.fnmatchcase(name, g) for g in claims.get("tool_globs") or [])


#: Tools whose `turn_id` argument must name a turn in this token's own
#: conversation.
TURN_PINNED = frozenset({"who_is_asking"})


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
            # (`who_is_asking`). The list is explicit rather than "any tool
            # with a turn_id argument", so a new tool is pinned by a person
            # deciding to pin it. (`site_tools`/`site_call` take no turn at
            # all: they read it from this token.)
            if name in TURN_PINNED:
                asked = str((context.message.arguments or {}).get("turn_id") or "")
                if asked not in set(claims.get("turn_ids") or []):
                    raise ToolError("turn not found")
        return await call_next(context)
