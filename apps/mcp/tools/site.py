"""`site_tools` / `site_call` / `site_read` — the host's tools and resources, as the
visitor, through canopy.

Host grant contract v1, §3 (`docs/architecture/host-grant-contract.md`). An agent
answering a visitor on a connected site does not call that site's MCP server
directly; it calls these, and canopy-web attaches the visitor's host-issued,
DPoP-bound token server-side (`apps/host_grants/host_gateway.py`). So:

* no runner ever holds a visitor's token or canopy's DPoP key;
* the owner's per-site ceiling is enforced HERE, server-side, not only by the
  runner's `profile_guard`;
* every call is one audit row, and none of them contains a token — or what the
  host answered: `site_call` records the tool, `site_read` the URI.

`site_read` is `resources/read` beside `site_call`'s `tools/call`: a host's tool may
answer with facts plus a `resource_link` to a result it keeps (spec
2026-10-23-host-held-results), and the agent reads the rows through it only when it
needs them — so a large result never has to be in context.

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
    from apps.host_grants import host_gateway

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
    from apps.host_grants import host_gateway

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
    from apps.host_grants import host_gateway

    ctx = await _context("site_call")
    # `app=<pk>` names the Connected site unambiguously (a site NAME is unique
    # only per tenant); the Connected sites table reads it for real-traffic health.
    summary = (f"turn={ctx.turn_id} site={ctx.site} app={ctx.app_id} "
               f"tool={host_gateway.host_tool_name(tool)}")
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


@mcp.tool
async def site_read(uri: str) -> dict:
    """Read a resource at the visitor's site, as the visitor — the rows behind a
    result a site tool returned as a link.

    A site tool may answer with facts (a row count, the columns, a few samples) and
    a `resource_link` whose `uri` names the full result the site keeps. Read it
    here only when you need more than the facts; a table pages with the site's own
    parameters (e.g. `?page=2&page_size=100`). The site decides, under the
    visitor's own permissions, whether they may read it; an expired or foreign
    result is refused — say so rather than guessing at its contents.
    """
    from apps.host_grants import host_gateway  # noqa: PLC0415 — as site_call: the gateway loads with a call

    ctx = await _context("site_read")
    # The URI only: never what came back (same as site_call, which records the tool).
    summary = f"turn={ctx.turn_id} site={ctx.site} app={ctx.app_id} uri={str(uri or '')[:200]}"
    try:
        out = await host_gateway.read_resource(ctx, uri)
    except host_gateway.GatewayRefusal as exc:
        await write_audit(user_id=current_user_id(), tool="site_read",
                          args_summary=summary, ok=False, error=exc.code)
        raise ToolError(exc.message) from None
    await write_audit(user_id=current_user_id(), tool="site_read", args_summary=summary, ok=True)
    return out
