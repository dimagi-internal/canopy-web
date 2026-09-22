"""`who_is_asking` — the caller envelope, re-readable mid-turn.

The runner writes the envelope beside the turn when it claims it (see
`apps/harness/caller_context.py`). This is the same document on demand, for an
agent that wants to check again rather than trust a file it read an hour ago —
a contact's notes may have been corrected, or the person blocked, since.

Gated exactly like `GET /api/harness/turns/{id}/caller-context`: the caller
must be a member of the turn's tenant, and a turn they cannot see is "not
found", never "forbidden", so a guessed id confirms nothing.
"""
from __future__ import annotations

import uuid

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from apps.mcp.audit import current_user_id, write_audit
from apps.mcp.server import mcp

User = get_user_model()


class TurnNotFound(LookupError):
    pass


def _tenant_of(turn) -> str | None:
    if turn.agent_id:
        return turn.agent.workspace_id
    if turn.chat_session_id:
        return turn.chat_session.workspace_id
    return turn.workspace_id


def _envelope_for_caller_token(turn_id: str) -> dict:
    """The middleware has already pinned `turn_id` to the token's conversation."""
    from apps.harness.caller_context import build
    from apps.harness.models import Turn

    turn = (Turn.objects.select_related("agent", "chat_session", "initiator_user",
                                        "initiator_contact").filter(pk=turn_id).first())
    if turn is None:
        raise TurnNotFound("turn not found")
    return build(turn)


def _envelope_sync(user_id, turn_id: str) -> dict:
    from apps.harness.caller_context import build
    from apps.harness.models import Turn
    from apps.workspaces import services as wsvc

    try:
        pk = uuid.UUID(str(turn_id))
    except ValueError:
        raise TurnNotFound("turn not found") from None
    turn = (Turn.objects.select_related("agent", "chat_session", "initiator_user",
                                        "initiator_contact")
            .filter(pk=pk).first())
    user = User.objects.filter(pk=user_id).first() if user_id is not None else None
    # Fail CLOSED on a tenant-less turn: no tenant must never read as "ungated".
    slug = _tenant_of(turn) if turn is not None else None
    if turn is None or user is None or not slug or not wsvc.is_member(user, slug):
        raise TurnNotFound("turn not found")
    return build(turn)


@mcp.tool
async def who_is_asking(turn_id: str) -> dict:
    """Who asked for this turn, how sure canopy is, and what it knows about them.

    Read this before acting on a request. `verified` is about THIS message: an
    address that once passed DMARC is not proof that today's message is not a
    forgery. `relationship` is what the asker is to the agent — owner, admin,
    member, caller (anyone outside, e.g. an email contact) or system. `contact`
    holds the workspace's notes and attributes for an outside person.
    """
    from apps.mcp.turn_scope import _turn_claims

    user_id = current_user_id()
    try:
        if _turn_claims() is not None:
            env = await sync_to_async(_envelope_for_caller_token, thread_sensitive=True)(turn_id)
        else:
            env = await sync_to_async(_envelope_sync, thread_sensitive=True)(user_id, turn_id)
    except Exception as exc:  # noqa: BLE001
        await write_audit(user_id=user_id, tool="who_is_asking",
                          args_summary=f"turn={turn_id}", ok=False, error=str(exc))
        raise
    await write_audit(user_id=user_id, tool="who_is_asking",
                      args_summary=f"turn={turn_id} -> {env['who'].get('kind')}", ok=True)
    return env
