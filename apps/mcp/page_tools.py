"""The page a user is looking at, exposed to the agent as ordinary MCP tools.

A host declares what its page can do in one JS call
(`registerAction(name, fn, {inputSchema})`); the declaration travels up over
the session; and this module turns it into tools the agent discovers and calls
like any other. The host never learns MCP, and any MCP-speaking agent gets the
tools — not only canopy's own.

**Why middleware rather than `@mcp.tool`.** canopy's other tools are static
Python functions, correct for capabilities canopy itself owns. These are
declared by a host at RUNTIME, differ per user, and vanish when a tab closes,
so the tool list has to be computed per caller. FastMCP's `on_list_tools` /
`on_call_tool` hooks are exactly that seam. An earlier draft of this feature
concluded MCP was the wrong fit; that was a judgement about canopy's current
IMPLEMENTATION, not about the protocol, and it was wrong.

**Why the browser is not the MCP server.** A page cannot accept an inbound
connection, so it cannot be one. canopy is the server and the page is the
executor behind it: canopy advertises, the agent calls, canopy rings the
session's socket, the page runs the callback and POSTs the result back.

**What MCP does not cover, and why the machinery underneath stays.** MCP has no
notion of a tool that lives in a tab which might be closed. That is what
`apps/canopy_sessions/page_actions.py` provides — a durable row, a doorbell,
and a bounded wait that refuses out loud. MCP is the interface; that is the
mechanism.
"""

from __future__ import annotations

import logging

from asgiref.sync import sync_to_async

log = logging.getLogger(__name__)

#: Prefix on every page-derived tool name. Namespaced so a host cannot shadow
#: a canopy tool — a page declaring `clear_insights` must not be able to
#: intercept calls meant for the real one.
TOOL_PREFIX = "page_"


def _attached_sessions(user):
    """Sessions belonging to `user` that currently declare page actions.

    A session only declares while a viewer is attached, so this is in practice
    "the pages this user has open". Ordered newest-first so the most recent
    attachment wins a name collision.
    """
    from apps.canopy_sessions.models import Session

    return list(
        Session.objects.filter(created_by=user, status=Session.ACTIVE)
        .exclude(page_actions_available=[])
        .order_by("-created_at")
    )


def page_tool_specs(user) -> list[tuple]:
    """`(tool_name, session, spec)` for everything this user's pages offer.

    A user with two tabs open on different pages has two candidate sets. The
    NEWEST attachment wins a duplicate name rather than both being exposed:
    two tools with one name is unusable, and "the page I just opened" is the
    better guess at which one the user means. The chosen session's id is in the
    tool description, so a wrong guess is visible rather than silent.
    """
    seen: dict[str, tuple] = {}
    for session in _attached_sessions(user):
        for spec in session.page_actions_available or []:
            name = f"{TOOL_PREFIX}{spec.get('name', '')}"
            if not spec.get("name") or name in seen:
                continue
            seen[name] = (name, session, spec)
    return list(seen.values())


def _describe(session, spec: dict) -> str:
    base = spec.get("description") or f"Run {spec.get('name')!r} on the page the user is viewing."
    return (
        f"{base}\n\n"
        f"Runs in the user's open browser tab (session {session.id}), as that user, "
        "so it can only do what they could. It fails if the page has been closed — "
        "it is never queued for later."
    )


def to_mcp_tool(name: str, session, spec: dict):
    """One page action as an MCP `Tool`.

    `inputSchema` is the host's own JSON-Schema, passed through. That is the
    whole reason a declaration carries a schema rather than just a name: an
    agent cannot call `dismissInsights` without being told it takes
    `{ids: number[]}`.
    """
    from mcp.types import Tool

    return Tool(
        name=name,
        description=_describe(session, spec),
        inputSchema=spec.get("inputSchema")
        or spec.get("parameters")
        or {"type": "object", "properties": {}},
    )


async def list_page_tools(user) -> list:
    if user is None:
        return []
    specs = await sync_to_async(page_tool_specs)(user)
    return [to_mcp_tool(name, session, spec) for name, session, spec in specs]


async def call_page_tool(user, name: str, arguments: dict):
    """Run one, and translate every refusal into something the agent can read.

    Refusals are returned as text rather than raised, because an MCP client
    shows a tool error to the model as content: "the page is closed" is
    actionable, an opaque exception is not.
    """
    from apps.canopy_sessions import page_actions

    specs = await sync_to_async(page_tool_specs)(user)
    match = next((s for s in specs if s[0] == name), None)
    if match is None:
        return None  # not ours; let the normal tool dispatch handle it
    _name, session, spec = match

    def run():
        return page_actions.request_action(
            session=session, name=spec["name"], args=arguments or {}, user=user
        )

    try:
        action = await sync_to_async(run)()
    except page_actions.PageActionError as exc:
        return {"isError": True, "text": f"{exc.code}: {exc.message}"}
    return {"isError": False, "result": action.result}
