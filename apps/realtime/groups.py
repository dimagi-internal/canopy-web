"""Group names, the turn membership gate, event serialization, and a null-safe
publish helper.

Pure functions (no socket, no async) so they unit-test without Channels. The one
impure helper, publish(), degrades to a no-op when no channel layer is configured
or a send fails — realtime is an enhancement, never a hard dependency of a write.

Note: the Workspace PK *is* the slug (workspaces.services.user_workspace_slugs
compares `workspace_id`), so a turn's tenant is read straight off `workspace_id`
without dereferencing the Workspace row.
"""
from __future__ import annotations

import logging
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from apps.harness.models import Turn, TurnEvent
from apps.workspaces.services import user_workspace_slugs

log = logging.getLogger(__name__)


def turn_group(turn_id: uuid.UUID | str) -> str:
    hexid = turn_id.hex if isinstance(turn_id, uuid.UUID) else uuid.UUID(str(turn_id)).hex
    return f"turn.{hexid}"


def supervisor_user_group(user_id: int) -> str:
    return f"supervisor.user.{user_id}"


def runner_group(runner_id) -> str:
    """Per-runner control-channel group — targeted server→runner frames (a
    cancel, a session interrupt). The runner's WS consumer joins this plus its
    workspaces' runnable groups."""
    hexid = runner_id.hex if hasattr(runner_id, "hex") else str(runner_id).replace("-", "")
    return f"runner.{hexid}"


def runnable_group(workspace_slug: str) -> str:
    """Per-tenant 'a turn is claimable' wake group. Runners long-polling /claim
    join their workspaces' runnable groups; enqueue publishes a wake here so a
    blocked claim returns immediately instead of waiting out its poll interval.
    Deliberately coarse (per-workspace, not per-agent): the wake only prompts a
    re-attempt — claim_next_turn still does all the capability/tenant/preference
    gating — so a small thundering herd (a few runners re-attempt, one wins) is
    fine and keeps the publish side from re-deriving eligibility."""
    return f"runnable.{workspace_slug}"


def session_group(session_id) -> str:
    """Per-session multiplayer group (SP3). A pure string helper so realtime's
    fan-out can target it without importing the chat app."""
    hexid = session_id.hex if hasattr(session_id, "hex") else str(session_id).replace("-", "")
    return f"chat.{hexid}"


def chat_user_group(user_id: int) -> str:
    """Every chat socket one person holds, across sessions. Lets a change to
    their ACCESS reach sockets already open — a removal from a workspace closes
    them now, rather than when the person next acts (`access.recheck`)."""
    return f"chat.user.{user_id}"


def turn_workspace_slug(turn: Turn) -> str | None:
    """The turn's tenant slug: from the agent (agent turns), the session (chat
    turns), or the turn's own workspace FK (project turns). None when unset."""
    if turn.agent_id:
        return turn.agent.workspace_id  # workspace PK == slug
    if turn.chat_session_id:
        return turn.chat_session.workspace_id
    return turn.workspace_id


def user_can_read_turn(user, turn: Turn) -> bool:
    """The same answer `GET /api/harness/turns/{id}` gives: the tenant, and for a
    session turn the chat's own ACL too. There is no superuser leg — a door that
    answers differently from its REST twin is how the session-turn leak happened.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    slug = turn_workspace_slug(turn)
    if not slug or slug not in user_workspace_slugs(user):
        return False
    # The stream IS the turn's content (its ledger), so it is read on the
    # content rule, which also covers a chat turn's chat ACL.
    from apps.harness import turn_access

    return turn_access.can_read_turn_content(user, turn)


def serialize_turn_event(te: TurnEvent) -> dict:
    return {"seq": te.seq, "kind": te.kind, "payload": te.payload, "ts": te.ts.isoformat()}


def publish(group: str, message: dict) -> None:
    """Fan a message out to a channel-layer group. Never raises: a missing layer
    or a send failure is logged and swallowed so a realtime hiccup can't break
    the write that triggered it."""
    try:
        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(group, message)
    except Exception:  # pragma: no cover - realtime must never break a write
        log.exception("realtime publish to %s failed", group)
