"""`agent_reply` — pick up an agent's answer to a capability tool you called.

A capability tool (`ace__ask`, from `apps/mcp/agent_tools.py`) waits a bounded
time for the agent; a turn is minutes of work, so the answer often lands after
the call returns. This reads it — the same ledger rows the web chat and the
Slack relay render — for a conversation the CALLER started. Nobody else's: a
conversation id is not a capability to read someone's exchange with an agent.
"""
from __future__ import annotations

import uuid

from asgiref.sync import sync_to_async

from apps.mcp.agent_tools import _wait, wait_for_reply
from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.server import mcp


class ConversationNotFound(LookupError):
    pass


def _latest_turn(user_id, conversation_id: str) -> str:
    from apps.canopy_sessions.models import Session
    from apps.harness.models import Turn

    try:
        pk = uuid.UUID(str(conversation_id))
    except ValueError:
        raise ConversationNotFound("conversation not found") from None
    session = Session.objects.filter(pk=pk, created_by_id=user_id).first() if user_id else None
    turn = (Turn.objects.filter(chat_session=session).order_by("-created_at").first()
            if session is not None else None)
    if turn is None:
        raise ConversationNotFound("conversation not found")
    return str(turn.pk)


@mcp.tool
async def agent_reply(conversation_id: str, wait_seconds: int = 0) -> dict:
    """The latest answer in a conversation you started with an agent's tool.

    Returns `status` (queued, running, waiting_on_you, done, failed…), the
    agent's `reply` so far, and — when it is waiting on you — its `question`.
    `wait_seconds` (≤ 110) waits for it to finish first.
    """
    user_id = current_user_id()
    try:
        turn_id = await sync_to_async(_latest_turn, thread_sensitive=True)(user_id, conversation_id)
    except ConversationNotFound as exc:
        await write_audit(user_id=user_id, tool="agent_reply",
                          args_summary=f"conversation={conversation_id}", ok=False, error=str(exc))
        raise
    state = await wait_for_reply(turn_id, _wait(wait_seconds))
    await write_audit(user_id=user_id, tool="agent_reply",
                      args_summary=f"conversation={conversation_id} -> {state['status']}", ok=True)
    return {"conversation_id": conversation_id, **state}
