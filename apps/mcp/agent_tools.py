"""Each agent's declared interface, served as MCP tools — computed for the caller.

Phase 4 of `docs/superpowers/specs/2026-09-18-who-is-asking-initiator-identity-
and-access-design.md` (§4): "canopy exposes each agent's interface as MCP tools
(`echo.ask`, `echo.summarise_opportunity`), computing the list for the caller
in front of it — the same thing `page_tools` already does. The call carries the
caller's token, so who is asking arrives with the request rather than being
inferred afterwards."

So `ace__ask` is not a proxy that runs ACE somewhere as canopy. It is a TURN:
the call opens (or continues) a conversation with the agent owned by the
caller, sends their message as THEM (initiator = their token's user), and the
turn runs in whatever profile the declared interface gives them — full for the
agent's owner and admins, confined to the capability for anyone else. The same
three layers that confine an emailer confine an MCP caller; this module adds a
door, not a second set of rules.

**Why a Provider.** The list differs per caller (which agents they are a member
of, which capabilities each offers their class) and changes when an interface
is published, so it is computed per request — `page_tools.PageActionProvider`
is the precedent. Static tools win name collisions over provider tools, and
every name here carries `__`, which no static tool uses.

**Turns take minutes; an MCP call should not.** A call waits up to
`wait_seconds` for the reply and otherwise returns `status: "running"` with a
`conversation_id` — `agent_reply` (a static tool in `tools/conversations.py`)
picks it up later. A turn that no live runner can take says so at once rather
than burning the wait.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid

from asgiref.sync import sync_to_async
from fastmcp.exceptions import ToolError
from fastmcp.server.providers.base import Provider
from fastmcp.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)

SEPARATOR = "__"
DEFAULT_WAIT = 60
MAX_WAIT = 110
_POLL = 2.0
_MAX_NAME = 64
_TERMINAL = {"done", "failed", "cancelled", "missed", "lost"}


def tool_name(agent_slug: str, capability: str) -> str | None:
    name = f"{agent_slug}{SEPARATOR}{capability}"
    # MCP tool names are [A-Za-z0-9_-]{1,64}; a slug that cannot fit is skipped
    # rather than truncated, which could collide two agents onto one name.
    return name if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) and len(name) <= _MAX_NAME else None


# --- listing -----------------------------------------------------------------------

def agent_tool_specs(user) -> list[tuple]:
    """`(tool_name, agent, capability_name, capability)` this user may call."""
    from apps.agents.interface import offered_to
    from apps.agents.models import Agent
    from apps.workspaces import services as wsvc

    slugs = wsvc.user_workspace_slugs(user)
    if not slugs:
        return []
    out = []
    for agent in (Agent.objects.filter(workspace_id__in=slugs).exclude(interface={})
                  .select_related("owner").order_by("slug")):
        caps = agent.interface.get("capabilities") or {}
        for name in offered_to(user, agent):
            tn = tool_name(agent.slug, name)
            if tn is not None:
                out.append((tn, agent, name, caps[name]))
    return out


def _schema(cap: dict, capability: str) -> dict:
    from apps.agents.interface import ASK, INPUT_TYPES

    props: dict = {
        "message": {"type": "string",
                    "description": "What you are asking, in your own words."},
        "conversation_id": {"type": "string",
                            "description": "Continue a conversation this tool returned earlier. "
                                           "Omit to start a new one."},
        "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT,
                         "description": f"How long to wait for the reply (default {DEFAULT_WAIT}). "
                                        "If the agent is still working, call agent_reply later."},
    }
    required = ["message"] if capability == ASK else []
    for field, typ in (cap.get("input") or {}).items():
        props[field] = {"type": INPUT_TYPES[typ]}
        required.append(field)
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def _describe(agent, capability: str, cap: dict) -> str:
    base = cap.get("description") or f"Ask {agent.name} ({capability})."
    return (f"{base}\n\nRuns as a turn of the agent `{agent.slug}`, asked by YOU: it acts "
            "within what its declared interface gives you, and its owner can see the "
            "conversation. Returns the reply, or `status: running` with a conversation_id — "
            "then call agent_reply.")


class AgentCapabilityTool(Tool):
    """One capability of one agent. Executes itself (the provider model), so the
    agent and capability travel on the tool — and are RE-AUTHORIZED on every
    call, because an interface can be unpublished between listing and calling."""

    agent_slug: str
    capability: str

    async def run(self, arguments: dict) -> ToolResult:
        from apps.mcp.audit import current_user_id, write_audit

        user_id = current_user_id()
        wait = _wait(arguments.get("wait_seconds"))
        try:
            session_id, turn_id = await sync_to_async(invoke, thread_sensitive=True)(
                user_id, self.agent_slug, self.capability, arguments or {})
        except InvocationError as exc:
            await write_audit(user_id=user_id, tool=self.name, args_summary=str(exc)[:200],
                              ok=False, error=str(exc))
            raise ToolError(str(exc)) from exc
        await write_audit(user_id=user_id, tool=self.name,
                          args_summary=f"conversation={session_id} turn={turn_id}", ok=True)
        state = await wait_for_reply(turn_id, wait)
        return ToolResult(structured_content={"conversation_id": session_id, **state})


def to_mcp_tool(name: str, agent, capability: str, cap: dict) -> AgentCapabilityTool:
    return AgentCapabilityTool(
        name=name, description=_describe(agent, capability, cap),
        parameters=_schema(cap, capability), agent_slug=agent.slug, capability=capability,
    )


class AgentInterfaceProvider(Provider):
    """Every agent's declared capabilities that the caller may invoke."""

    async def _list_tools(self):
        from .page_tools import _current_user

        user = await sync_to_async(_current_user, thread_sensitive=True)()
        if user is None:
            return []
        specs = await sync_to_async(agent_tool_specs, thread_sensitive=True)(user)
        return [to_mcp_tool(*s) for s in specs]


# --- invoking ----------------------------------------------------------------------

class InvocationError(Exception):
    pass


def _wait(value) -> int:
    try:
        return max(0, min(MAX_WAIT, int(DEFAULT_WAIT if value is None else value)))
    except (TypeError, ValueError):
        return DEFAULT_WAIT


def _check_inputs(cap: dict, arguments: dict) -> dict:
    typed = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
    out = {}
    for field, typ in (cap.get("input") or {}).items():
        if field not in arguments:
            raise InvocationError(f"missing required input '{field}'")
        v = arguments[field]
        if isinstance(v, bool) and typ in ("integer", "number"):
            raise InvocationError(f"input '{field}' must be {typ}")
        if not isinstance(v, typed[typ]):
            raise InvocationError(f"input '{field}' must be {typ}")
        out[field] = v
    return out


def invoke(user_id, agent_slug: str, capability: str, arguments: dict) -> tuple[str, str]:
    """Send the caller's request as a turn of the agent. Returns (conversation, turn)."""
    from django.contrib.auth import get_user_model

    from apps.agents.interface import ASK, offered_to
    from apps.agents.models import Agent
    from apps.canopy_sessions import services as chat
    from apps.canopy_sessions.models import Session
    from apps.harness import initiator as who
    from apps.harness.models import Turn
    from apps.mcp.rate_limit import RateLimitError, check_write_limit

    user = get_user_model().objects.filter(pk=user_id, is_active=True).first() if user_id else None
    agent = Agent.objects.filter(slug=agent_slug).select_related("workspace").first()
    # Re-authorized on every call: listing is not permission, and an interface
    # can be unpublished (or the caller removed) between list and call. The same
    # answer for "no such agent" and "not offered to you", so a caller learns
    # nothing about agents outside their reach.
    if user is None or agent is None or capability not in offered_to(user, agent):
        raise InvocationError(f"{agent_slug}{SEPARATOR}{capability} is not available to you")
    try:
        check_write_limit(user.pk)
    except RateLimitError as exc:
        raise InvocationError(str(exc)) from exc

    cap = (agent.interface.get("capabilities") or {}).get(capability) or {}
    message = str(arguments.get("message") or "").strip()
    inputs = _check_inputs(cap, arguments)
    if capability == ASK and not message:
        raise InvocationError("message is required")
    prompt = message
    if inputs:
        block = json.dumps(inputs, indent=2, sort_keys=True)
        prompt = (f"{message}\n\n" if message else "") + (
            f"[{capability} — inputs]\n```json\n{block}\n```")
    if not prompt:
        raise InvocationError("nothing to send: give a message or the inputs")

    conv = str(arguments.get("conversation_id") or "").strip()
    if conv:
        try:
            session = Session.objects.filter(pk=uuid.UUID(conv), agent=agent,
                                             created_by=user).first()
        except ValueError:
            session = None
        if session is None:
            raise InvocationError("no such conversation of yours with this agent")
    else:
        session = chat.create_session(
            workspace=agent.workspace, created_by=user, agent=agent,
            title=(message or capability)[:80],
            metadata={"via": "mcp", "capability": capability},
        )
    _msg, turn = chat.send_message(
        session=session, text=prompt, user=user, client_id=uuid.uuid4().hex,
        origin=Turn.ORIGIN_API, capability=capability,
        initiator=who.for_user(user, via=f"mcp:{capability}", assurance=who.PAT),
    )
    if turn is None:
        raise InvocationError("the message could not be sent")
    chat.maybe_execute_inline(turn)
    return str(session.pk), str(turn.pk)


# --- reading the reply -------------------------------------------------------------

def reply_state(turn_id: str) -> dict:
    """What the caller should see about a turn right now. Reads the same ledger
    rows the web chat and the Slack relay render: `assistant` rows are the
    reply; a failed turn says why; a blocked agent's question is surfaced so the
    caller can answer it with another message."""
    from apps.canopy_sessions.serializers import pending_menu
    from apps.harness import services as harness
    from apps.harness.models import Turn

    turn = (Turn.objects.select_related("chat_session", "chat_session__runner_binding")
            .filter(pk=turn_id).first())
    if turn is None:
        return {"status": "missing", "turn_id": turn_id, "reply": ""}
    reply = "\n\n".join(
        str((e.payload or {}).get("text") or "").strip()
        for e in turn.events.filter(kind="assistant").order_by("seq")
        if str((e.payload or {}).get("text") or "").strip())
    out = {"turn_id": str(turn.pk), "status": turn.status, "reply": reply}
    if turn.status in ("failed", "cancelled", "missed", "lost") and turn.result_note:
        out["note"] = turn.result_note
    menu = pending_menu(turn.chat_session) if turn.chat_session_id else None
    if menu and turn.status not in _TERMINAL:
        out["status"] = "waiting_on_you"
        out["question"] = menu.get("question") or ""
        out["options"] = [o.get("label") or o for o in (menu.get("options") or [])]
        out["note"] = "The agent is asking you something: answer by calling the tool again " \
                      "with this conversation_id and your answer as the message."
    if turn.status == Turn.QUEUED:
        reach = harness.turn_reach(turn)
        if reach.kind != harness.LIVE:
            out["note"] = ("No online runner can take this yet — it will run when one can."
                           if reach.kind == harness.OFFLINE else
                           "No runner serves this agent for you right now; its owner has been "
                           "told through canopy's unclaimable-turns warning.")
    return out


async def wait_for_reply(turn_id: str, wait: int) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while True:
        state = await sync_to_async(reply_state, thread_sensitive=True)(turn_id)
        if state["status"] in _TERMINAL or state["status"] == "waiting_on_you" \
                or loop.time() >= deadline:
            if state["status"] not in _TERMINAL and state["status"] != "waiting_on_you":
                state["status"] = "running" if state["status"] != "queued" else "queued"
            return state
        # A queued turn nobody can take will not start inside the wait; say so now.
        if state["status"] == "queued" and "note" in state:
            return state
        await asyncio.sleep(_POLL)
