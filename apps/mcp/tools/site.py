"""`site_tools` / `site_call` — the host's tools, as the visitor, through canopy.

Host grant contract v1, §3 (`docs/architecture/host-grant-contract.md`). An agent
answering a visitor on a connected site does not call that site's MCP server
directly; it calls these, and canopy-web attaches the visitor's host-issued,
DPoP-bound token server-side (`apps/tokens/host_gateway.py`). So:

* no runner ever holds a visitor's token or canopy's DPoP key;
* the owner's per-site ceiling is enforced HERE, server-side, not only by the
  runner's `profile_guard`;
* every call is one audit row, and none of them contains a token.

**Caller-token only.** These exist for a CONFINED visitor's session, whose token
names its own conversation: the turn is read from that token, never from an
argument, so a session cannot point the gateway at somebody else's visitor. A
PAT (the owner in their own session) gets a refusal — the owner has their own
login at the site and nothing to borrow.
"""
from __future__ import annotations

from asgiref.sync import sync_to_async
from fastmcp.exceptions import ToolError

from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.server import mcp


def _current_turn_id() -> str:
    from apps.mcp.turn_scope import _turn_claims

    claims = _turn_claims()
    if claims is None or not claims.get("turn_id"):
        raise ToolError("site tools exist only in a visitor's session")
    return str(claims["turn_id"])


async def _context(tool: str):
    from apps.tokens import host_gateway

    turn_id = _current_turn_id()
    try:
        return await sync_to_async(host_gateway.resolve, thread_sensitive=True)(turn_id)
    except host_gateway.GatewayRefusal as exc:
        await write_audit(user_id=current_user_id(), tool=tool,
                          args_summary=f"turn={turn_id}", ok=False, error=exc.code)
        # ToolError: its message always reaches the agent, which is the point —
        # "I need you back on the page" is what the agent should say.
        raise ToolError(exc.message) from None


@mcp.tool
async def site_tools() -> dict:
    """The tools of the site this visitor is on that you may use for them.

    Each call you make through `site_call` runs AS THE VISITOR at that site —
    their own permissions, not yours — so a tool listed here can still refuse
    something they are not allowed to do. Only tools the page they are on
    offers, and that the agent's owner allowed for this site, are listed.

    Refused when the visitor has left the page (their access is short-lived and
    never renewed without them): tell them you need them back on the page.
    """
    from apps.tokens import host_gateway

    ctx = await _context("site_tools")
    try:
        tools = await host_gateway.list_tools(ctx)
    except host_gateway.GatewayRefusal as exc:
        await write_audit(user_id=current_user_id(), tool="site_tools",
                          args_summary=f"turn={ctx.turn_id} site={ctx.site}",
                          ok=False, error=exc.code)
        raise ToolError(exc.message) from None
    await write_audit(user_id=current_user_id(), tool="site_tools",
                      args_summary=f"turn={ctx.turn_id} site={ctx.site} -> {len(tools)} tools",
                      ok=True)
    return {"site": ctx.site, "scope": ctx.scope, "tools": tools}


@mcp.tool
async def site_call(tool: str, arguments: dict | None = None) -> dict:
    """Call one of `site_tools` at the visitor's site, as the visitor.

    `tool` is the name `site_tools` listed. The result is the site's own answer;
    `is_error` true means the site refused or failed, which usually means the
    visitor is not allowed that at the site — say so rather than retrying.
    """
    from apps.tokens import host_gateway

    ctx = await _context("site_call")
    summary = f"turn={ctx.turn_id} site={ctx.site} tool={host_gateway.host_tool_name(tool)}"
    if not ctx.allows(tool):
        await write_audit(user_id=current_user_id(), tool="site_call",
                          args_summary=summary, ok=False, error="not_allowed")
        raise ToolError(f"{tool} is not something you may use on {ctx.site} here")
    try:
        out = await host_gateway.call_tool(ctx, tool, arguments or {})
    except host_gateway.GatewayRefusal as exc:
        await write_audit(user_id=current_user_id(), tool="site_call",
                          args_summary=summary, ok=False, error=exc.code)
        raise ToolError(exc.message) from None
    await write_audit(user_id=current_user_id(), tool="site_call",
                      args_summary=f"{summary} is_error={out['is_error']}", ok=True)
    return out
