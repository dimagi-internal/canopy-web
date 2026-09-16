"""The page a user is looking at, exposed to the agent as ordinary MCP tools.

A host declares what its page can do in one JS call
(`registerAction(name, fn, {description, parameters})`, where `parameters` is
JSON-Schema and `inputSchema` is accepted as MCP's name for the same thing);
the declaration travels up over the session; and this module turns it into
tools the agent discovers and calls like any other. The host never learns MCP,
and any MCP-speaking agent gets the tools — not only canopy's own.

**Why a Provider rather than `@mcp.tool`.** canopy's other tools are static
Python functions, correct for capabilities canopy itself owns. These are
declared by a host at RUNTIME, differ per user, and vanish when a tab closes,
so the tool list has to be computed per caller. FastMCP 3's `Provider` is
exactly that seam — `add_provider` is documented as "a provider for dynamic
tools" — and it carries a second guarantee for free: "static components
registered via decorators always take precedence over providers", so a page
cannot capture a canopy tool even if the `page_` prefix were ever dropped. An
earlier draft of this feature concluded MCP was the wrong fit; that was a
judgement about canopy's IMPLEMENTATION, not about the protocol, and it was
wrong.

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
from fastmcp.exceptions import ToolError
from fastmcp.server.providers.base import Provider
from fastmcp.tools.tool import Tool, ToolResult

log = logging.getLogger(__name__)

#: Prefix on every page-derived tool name. Namespaced so a host cannot shadow
#: a canopy tool — a page declaring `clear_insights` must not be able to
#: intercept calls meant for the real one.
TOOL_PREFIX = "page_"


def _attached_sessions(user):
    """Sessions whose page `user` may act on, that currently declare actions.

    A session only declares while a viewer is attached, so this is in practice
    "the pages this caller can reach". Ordered newest-first so the most recent
    attachment wins a name collision.

    The predicate is `page_access.page_visible_q`, not `created_by=user`. That
    older filter was right for a human asking about their own tabs and wrong for
    the only case that happens in production: the AGENT is the caller, holding
    its own PAT, while the session was created by the human it is talking to —
    so it matched nothing and the agent silently had no page tools at all.
    """
    from apps.canopy_sessions.page_access import sessions_with_page_for

    return list(sessions_with_page_for(user).exclude(page_actions_available=[]))


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


class PageActionTool(Tool):
    """A tool whose implementation is a browser tab.

    Subclasses FastMCP's `Tool` and overrides `run` — components "execute
    themselves" under the provider model, so the session and the host's own
    action name travel on the tool rather than being looked up again at call
    time. That also closes a race worth naming: between listing and calling,
    the user may have navigated, and re-resolving by name would silently run
    whatever the NEW page happens to call the same thing.
    """

    session_id: str
    action_name: str

    async def run(self, arguments: dict) -> ToolResult:
        from apps.canopy_sessions import page_actions
        from apps.canopy_sessions.models import Session

        def go():
            session = Session.objects.filter(id=self.session_id).first()
            if session is None:
                raise page_actions.PageActionError("no_page", "that page is no longer open")
            return page_actions.request_action(
                session=session,
                name=self.action_name,
                args=arguments or {},
                user=session.created_by,
            )

        try:
            action = await sync_to_async(go, thread_sensitive=True)()
        except page_actions.PageActionError as exc:
            # A refusal must reach the MODEL as words it can act on. `ToolError`
            # is what FastMCP renders into `isError` content; letting the
            # exception escape would surface an opaque internal error instead,
            # and "the tab is closed" is precisely the outcome an agent needs to
            # be able to respond to.
            raise ToolError(f"{exc.code}: {exc.message}") from exc
        return ToolResult(structured_content={"result": action.result})


def to_mcp_tool(name: str, session, spec: dict) -> PageActionTool:
    """One page action as a tool.

    The schema is the host's own JSON-Schema, passed through. That is the whole
    reason a declaration carries one rather than just a name: an agent cannot
    call `dismissInsights` without being told it takes `{ids: number[]}`.

    Both spellings are accepted — `inputSchema` is MCP's and `parameters` is
    OpenAI function-calling's — because a host author will reasonably write
    either and a schemaless tool is worse than a wrong one.
    """
    return PageActionTool(
        name=name,
        description=_describe(session, spec),
        parameters=spec.get("inputSchema")
        or spec.get("parameters")
        or {"type": "object", "properties": {}},
        session_id=str(session.id),
        action_name=spec["name"],
    )


class PageActionProvider(Provider):
    """Every open page's actions, for whoever is asking.

    `_list_tools` takes no caller argument, so the user comes from the MCP
    request's own access token (`current_user_id`) — the same resolution the
    static tools use. Outside a request there is no user and the provider
    contributes nothing, which is the right answer rather than an error: the
    server lists tools in contexts that have no caller.
    """

    async def _list_tools(self):
        user = await sync_to_async(_current_user, thread_sensitive=True)()
        if user is None:
            return []
        specs = await sync_to_async(page_tool_specs, thread_sensitive=True)(user)
        return [to_mcp_tool(name, session, spec) for name, session, spec in specs]


def _current_user():
    """The Django user behind this MCP request, or None outside one."""
    from django.contrib.auth.models import User

    from apps.mcp.audit import current_user_id

    try:
        user_id = current_user_id()
    except Exception:  # noqa: BLE001 - no request context is not an error here
        return None
    if not user_id:
        return None
    return User.objects.filter(pk=user_id).first()
