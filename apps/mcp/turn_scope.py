"""A confined session's MCP calls reach canopy as `agent ∩ caller`.

Who-is-asking §7. A caller token (harness.caller_tokens) already makes the tools
run as the CALLER — their own ACL, or none for an outside contact. This
middleware adds the agent half: only the canopy tools the caller's capability
lists exist for that session. Listing and calling are both filtered, and
`who_is_asking` answers only about the session's own conversation.

A FULL-profile caller token for an AGENT's turn (`read_only`: an editor's turn, a
`full:` rule's, a writer's in someone else's chat) sees every tool that only READS,
run as the asker, plus driving their own open page, and no write: the asker granted
the agent no act-as-you permission (`READ_ONLY_REFUSAL`). It writes as the agent's own
login. A repo chat's token (no agent) is the person's own tool: every tool, as them.

Everything else — a PAT, the widget's tokens — passes through untouched.
"""
from __future__ import annotations

import fnmatch

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

from apps.agents.sensitive_data import sensitive_tool
from apps.harness.caller_tokens import PAGE_DRIVE_TOOLS, PAGE_TOOL_PREFIX
from apps.tokens import kinds


def _turn_claims() -> dict | None:
    try:
        tok = get_access_token()
    except Exception:  # noqa: BLE001 - no request context
        return None
    claims = (tok.claims or {}) if tok is not None else {}
    return claims if claims.get("auth_method") == kinds.CALLER_TOKEN else None


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


#: What a full-profile caller token's session is told when it tries a write. Its tools
#: run as the person who asked, and nothing that person did lets the agent act as them.
READ_ONLY_REFUSAL = (
    "{name} changes something, and this session reaches canopy as the person who asked "
    "only to read: the agent can't act as them here, because they haven't granted it an "
    "act-as-you permission. Do it as the agent's own login — the `canopy` CLI acts as the "
    "agent — or ask them to do it.")


def read_only(tool) -> bool:
    """A tool that only reads: its MCP annotation says so (`api_tools._annotations`
    sets it for every GET route; a hand-written tool declares it)."""
    ann = getattr(tool, "annotations", None)
    return bool(ann is not None and getattr(ann, "readOnlyHint", False))


def _allowed(name: str, claims: dict, tool=None) -> bool:
    if claims.get("sensitive"):
        # A sensitive turn's session: the canopy tools a `data: sensitive`
        # capability may hold (and its page's own actions), never more, whatever
        # else the claims say (`caller_tokens._sensitive_grant` already narrowed
        # them; this is the second line).
        if not sensitive_tool(name):
            return False
        return any(fnmatch.fnmatchcase(name, g) for g in claims.get("tool_globs") or [])
    if claims.get("all_tools"):
        if not claims.get("read_only"):
            return True         # a repo chat: the person's own tool, every tool as them
        # An agent's full-profile turn asked by someone else (an editor, a `full:` rule,
        # a writer in someone else's chat): every READ, run as the asker, and driving
        # their own open page (`caller_tokens.PAGE_DRIVE_TOOLS`). Never a write: the
        # asker granted the agent no act-as-you permission, so it may not act as them.
        if name in PAGE_DRIVE_TOOLS or name.startswith(PAGE_TOOL_PREFIX):
            return True
        return tool is not None and read_only(tool)
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
        return [t for t in tools if _allowed(t.name, claims, t)]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        claims = _turn_claims()
        if claims is not None:
            name = context.message.name
            tool = None
            if claims.get("read_only") and not claims.get("sensitive"):
                tool = await _tool(context, name)
            if not _allowed(name, claims, tool):
                if tool is not None:
                    raise ToolError(READ_ONLY_REFUSAL.format(name=name))
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


async def _tool(context: MiddlewareContext, name: str):
    """The tool being called, for its annotations; None when it cannot be found."""
    try:
        return await context.fastmcp_context.fastmcp.get_tool(name)
    except Exception:  # noqa: BLE001 — unknown means "not a read", which refuses
        return None
