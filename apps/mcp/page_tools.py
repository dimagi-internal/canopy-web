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
import time

from asgiref.sync import sync_to_async
from fastmcp.exceptions import ToolError
from fastmcp.server.providers.base import Provider
from fastmcp.tools.base import Tool, ToolResult

from apps.harness import sensitivity
from apps.mcp.turn_scope import caller_turn_ids

log = logging.getLogger(__name__)

#: Prefix on every page-derived tool name. Namespaced so a host cannot shadow
#: a canopy tool — a page declaring `act_on_task` must not be able to
#: intercept calls meant for the real one.
TOOL_PREFIX = "page_"


def _attached_sessions(user):
    """Sessions whose page `user` may act on, that currently declare actions.

    A session only declares while a viewer is attached, so this is in practice
    "the pages this caller can reach". Ordered newest-first so the most recent
    attachment wins a name collision.

    This is the USER path: a person's own tabs (`page_access.page_visible_q`).
    An agent's session reaches the page of the chat it is driving through that
    chat's key instead (`_list_tools` checks it first) — its login alone is in
    every chat the agent is in, so it matches nothing here.
    """
    from apps.canopy_sessions.page_access import sessions_with_page_for

    # MCP is never a sensitive conversation's chat surface (`sensitivity.reads_content`):
    # its page is acted on only by the turn running in it, through its chat key.
    return [session for session in sessions_with_page_for(user).exclude(page_actions_available=[])
            if sensitivity.reads_content(session, chat_surface=False, user=user)]


def conversation_sessions(turn_ids: list[str]) -> list:
    """The conversation a confined caller token names, when it declares actions.

    A confined turn's session presents its caller token, not a chat key, and its
    tools run as the CALLER — a visitor who may have no canopy account, so the user
    path matches nothing (and for a member would offer every tab they have open).
    The token already names its conversation, minted by canopy for this turn: that
    is the page it acts on, and nothing else (as `current_page._pages_of_turns`).
    """
    from apps.canopy_sessions.models import Session  # noqa: PLC0415 — models: imported before apps load
    from apps.harness.models import Turn  # noqa: PLC0415 — models: imported before apps load

    ids = {sid for sid in Turn.objects.filter(pk__in=turn_ids)
           .values_list("chat_session_id", flat=True) if sid}
    return list(Session.objects.filter(pk__in=ids).exclude(page_actions_available=[])
                .order_by("-created_at"))


def declared_tool_names(turn_ids: list[str]) -> set[str]:
    """The `page_*` names a confined token's own conversation offers right now."""
    return {name for name, _session, _spec in _specs_of(conversation_sessions(turn_ids))}


def page_tool_specs(user) -> list[tuple]:
    """`(tool_name, session, spec)` for everything this user's pages offer.

    A user with two tabs open on different pages has two candidate sets. The
    NEWEST attachment wins a duplicate name rather than both being exposed:
    two tools with one name is unusable, and "the page I just opened" is the
    better guess at which one the user means. The chosen session's id is in the
    tool description, so a wrong guess is visible rather than silent.
    """
    return _specs_of(_attached_sessions(user))


def _specs_of(sessions) -> list[tuple]:
    seen: dict[str, tuple] = {}
    for session in sessions:
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
            # Asked again at the call, not only at the listing: a conversation can turn
            # sensitive in between, and then only the turn running in it — the request
            # carrying its chat key, or its own confined caller token — may act on
            # its page.
            if sensitivity.is_sensitive(session) and not _is_own_conversation(session):
                raise page_actions.PageActionError("refused", sensitivity.NOT_IN_IT_PAGE)
            return page_actions.begin_action(
                session=session,
                name=self.action_name,
                args=arguments or {},
                user=session.created_by,
            )

        try:
            action = await sync_to_async(go, thread_sensitive=True)()
            # The wait, one poll at a time, each its own short call: the shared sync
            # thread is where the chat socket delivers this action's doorbell, and
            # held for the whole wait the page heard it only once the wait had
            # expired. It ran, answered within a second, and found the row expired.
            timeout = page_actions.DEFAULT_TIMEOUT_SECONDS
            deadline = time.monotonic() + timeout
            settled = None
            while settled is None and time.monotonic() < deadline:
                settled = await sync_to_async(page_actions.wait_once, thread_sensitive=True)(action)
            if settled is None:
                await sync_to_async(page_actions.expire, thread_sensitive=True)(action, timeout)
            action = settled
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
    call `declineTasks` without being told it takes `{ids: string[]}`.

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
        # A chat key narrows it to that one chat's page: an agent's login is in
        # every chat the agent is in, and "the page I am talking about" is this one.
        chat = await sync_to_async(_current_chat, thread_sensitive=True)()
        if chat is not None:
            specs = _specs_of([chat])
            return [to_mcp_tool(name, session, spec) for name, session, spec in specs]
        # A confined caller token names its own conversation: that page, only.
        turn_ids = _caller_turn_ids()
        if turn_ids is not None:
            sessions = await sync_to_async(conversation_sessions, thread_sensitive=True)(turn_ids)
            return [to_mcp_tool(name, session, spec) for name, session, spec in _specs_of(sessions)]
        user = await sync_to_async(_current_user, thread_sensitive=True)()
        if user is None:
            return []
        specs = await sync_to_async(page_tool_specs, thread_sensitive=True)(user)
        return [to_mcp_tool(name, session, spec) for name, session, spec in specs]


def _caller_turn_ids() -> list[str] | None:
    return caller_turn_ids()


def _is_own_conversation(session) -> bool:
    """Whether this request is the turn running in `session`: its chat key, or a
    confined caller token whose conversation it is."""
    chat = _current_chat()
    if chat is not None:
        return chat.pk == session.pk
    from apps.harness.models import Turn  # noqa: PLC0415 — models: imported before apps load

    turn_ids = _caller_turn_ids()
    return bool(turn_ids) and Turn.objects.filter(pk__in=turn_ids, chat_session=session).exists()


def _current_chat():
    try:
        from apps.mcp.chat_scope import current_chat_session

        return current_chat_session()
    except Exception:  # noqa: BLE001 - no request context is not an error here
        return None


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
