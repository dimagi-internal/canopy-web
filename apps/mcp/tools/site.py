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

**The turn comes from a credential, never an argument.** A confined (or scoped)
session's caller token names its own conversation; a full-profile turn — the
owner's, an admin's — reaches canopy as the agent's own login and names its
conversation with the chat key the runner handed only to that session. So a
session cannot point the gateway at somebody else's conversation. A chat held on a
site's page reaches that site only this way, as the person who sent — the owner
included: the agent's own login there is withheld from the turn
(`interface.own_login_of`). A PAT with no chat key (no page conversation) is refused.
"""
from __future__ import annotations

from asgiref.sync import sync_to_async
from fastmcp.exceptions import ToolError

from apps.mcp import page_tools
from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.server import mcp
from apps.mcp.turn_scope import caller_turn_ids


NOT_ON_A_PAGE = ("site tools exist only in a conversation held on a site's page, where they "
                 "act as the person who sent")


def _chat_turn_id() -> str:
    """The turn running in the chat this request's chat key names, or "".

    A full-profile turn (the owner's, an admin's) reaches canopy as the agent's own
    login, which every chat it is in shares; the chat key (`X-Canopy-Chat-Key`, minted
    when the runner claimed the chat's turn and handed only to the session driving it)
    is what says WHICH conversation — so the turn is still read from a credential,
    never from an argument. The chat's latest turn is the one running in it."""
    from apps.harness.models import Turn  # noqa: PLC0415 — models load with a call
    from apps.mcp.chat_scope import current_chat_session  # noqa: PLC0415 — as page_tools: reads models, loaded with a call

    try:
        session = current_chat_session()
    except Exception:  # noqa: BLE001 — no request in scope
        return ""
    if session is None:
        return ""
    turn = Turn.objects.filter(chat_session=session).order_by("-created_at").only("pk").first()
    return str(turn.pk) if turn is not None else ""


def _current_turn_id() -> str:
    """The turn this call is for: a caller token's own (a confined or scoped session),
    else the turn of the chat its chat key names (a full-profile turn on a page)."""
    from apps.mcp.turn_scope import _turn_claims

    claims = _turn_claims()
    if claims is not None:
        if not claims.get("turn_id"):
            raise ToolError(NOT_ON_A_PAGE)
        return str(claims["turn_id"])
    turn_id = _chat_turn_id()
    if not turn_id:
        raise ToolError(NOT_ON_A_PAGE)
    return turn_id


async def _context(tool: str, reason: str = ""):
    from apps.host_grants import host_gateway

    turn_id = await sync_to_async(_current_turn_id, thread_sensitive=True)()
    try:
        return await sync_to_async(host_gateway.resolve, thread_sensitive=True)(turn_id, reason=reason)
    except host_gateway.GatewayRefusal as exc:
        await write_audit(user_id=current_user_id(), tool=tool,
                          args_summary=f"turn={turn_id}", ok=False, error=exc.code)
        # ToolError: its message always reaches the agent, which is the point —
        # "I need you back on the page" is what the agent should say.
        raise ToolError(exc.message) from None


def _page_action_named(tool: str) -> str:
    """The `page_*` tool `tool` names when it is one of the actions this conversation's
    page declares (written `page_<name>`, with or without an MCP server prefix), else "".

    Only a name the page declares matches: a site may have tools of its own whose
    names start `page_` (Labs' `page_context`), and those still go to the site. A bare
    action name is left alone too — it could as well be one of the site's tools."""
    from apps.host_grants import host_gateway  # noqa: PLC0415 — as site_call: the gateway loads with a call

    turn_ids = caller_turn_ids()
    if not turn_ids:
        return ""
    declared = page_tools.declared_tool_names(turn_ids)
    name = host_gateway.host_tool_name(tool)
    return name if name.startswith(page_tools.TOOL_PREFIX) and name in declared else ""


@mcp.tool(annotations={"readOnlyHint": True})
async def site_tools() -> dict:
    """The tools of the site this visitor is on that you may use for them.

    Each call you make through `site_call` runs AS THE VISITOR at that site —
    their own permissions, not yours — so a tool listed here can still refuse
    something they are not allowed to do. Only tools the page they are on
    offers, and that the agent's owner allowed for this site, are listed.

    Refused when the visitor has left the page (their access is short-lived and
    never renewed without them): tell them you need them back on the page.

    What the PAGE itself can do (show a query, open a row, fill a form) is not
    here: those are canopy's own `page_*` tools, called directly — never through
    `site_call`.
    """
    from apps.host_grants import host_gateway

    ctx = await _context("site_tools", "see what it can do for you on this page")
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
async def site_call(tool: str, arguments: dict | None = None, reason: str = "") -> dict:
    """Call one of `site_tools` at the visitor's site, as the visitor.

    `tool` is the name `site_tools` listed. The result is the site's own answer;
    `is_error` true means the site refused or failed, which usually means the
    visitor is not allowed that at the site — say so rather than retrying.

    `reason`: one short, plain sentence for the person saying what you are about to
    do as them ("close the 3 sessions you picked"). Until they confirm with their
    passkey that you may act as them on this page, the call is refused and they are
    asked in their panel, with this reason in front of them — so make it specific
    and honest.
    """
    from apps.host_grants import host_gateway

    page_tool = await sync_to_async(_page_action_named, thread_sensitive=True)(tool)
    if page_tool:
        # Not the site's: one of the actions the person's PAGE declared, which runs in
        # their tab through canopy's own tool of that name. Said by name so a wrong
        # guess corrects itself instead of reading as the site refusing the person.
        await write_audit(user_id=current_user_id(), tool="site_call",
                          args_summary=f"tool={host_gateway.host_tool_name(tool)}",
                          ok=False, error="page_action")
        raise ToolError(
            f"{tool} is an action of the page the person is on, not one of the site's tools: "
            f"call canopy's own tool `{page_tool}` directly (load it with ToolSearch if it is "
            "not in your list). It runs in their open tab, as them.")
    ctx = await _context("site_call", reason or f"use {host_gateway.host_tool_name(tool)}")
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


@mcp.tool(annotations={"readOnlyHint": True})
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

    ctx = await _context("site_read", "read a result it fetched for you")
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
