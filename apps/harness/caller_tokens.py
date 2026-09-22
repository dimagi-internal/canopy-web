"""Mint and resolve CallerTokens — see `models.CallerToken` for why they exist."""
from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import secrets
from dataclasses import dataclass, field

from django.utils import timezone

from .models import CallerToken, Turn

PREFIX = "cct_"
#: A conversation's token outlives turns but not forever.
LIFETIME = dt.timedelta(days=7)
#: A finished turn's session may still be settling (the reply streaming back).
AFTER_FINISH = dt.timedelta(minutes=10)
#: The MCP server name as Claude Code mounts it — `mcp__<mount>canopy-web__<tool>`.
_SERVER = "canopy-web__"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def mint(turn: Turn) -> str:
    """A token for this confined turn's conversation. Returned once, never stored raw."""
    raw = PREFIX + secrets.token_urlsafe(32)
    CallerToken.objects.create(token_hash=_hash(raw), turn=turn, chat_session_id=turn.chat_session_id,
                               expires_at=timezone.now() + LIFETIME)
    return raw


@dataclass
class Grant:
    """What a caller token lets a session do on canopy's MCP, right now."""

    turn: Turn
    user: object | None          # the asker when they are a canopy user; None for a contact
    tool_globs: list[str] = field(default_factory=list)
    turn_ids: set[str] = field(default_factory=set)

    def allows_tool(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, g) for g in self.tool_globs)


def canopy_tool_globs(capability: dict | None) -> list[str]:
    """The canopy MCP tools a capability lists, as globs over canopy's own tool
    names: `mcp__*canopy-web__who_is_asking` → `who_is_asking`."""
    out = []
    for pattern in (capability or {}).get("tools") or []:
        if _SERVER in pattern:
            out.append(pattern.split(_SERVER, 1)[1])
    return out


def resolve(raw: str) -> Grant | None:
    """The grant behind a raw token, or None. Fails closed on anything odd."""
    from apps.agents.interface import profile

    if not raw or not raw.startswith(PREFIX):
        return None
    tok = (CallerToken.objects.select_related("turn", "chat_session")
           .filter(token_hash=_hash(raw)).first())
    now = timezone.now()
    if tok is None or tok.expires_at <= now:
        return None
    if tok.chat_session_id:
        turns = Turn.objects.filter(chat_session_id=tok.chat_session_id)
        current = turns.select_related("agent", "chat_session__agent", "initiator_user") \
                       .order_by("-created_at").first()
        turn_ids = {str(t) for t in turns.values_list("pk", flat=True)}
    else:
        current, turn_ids = tok.turn, {str(tok.turn_id)}
    if current is None or not current.capability:
        # A confined session's conversation should only ever hold confined turns;
        # if the current one is not, this token stands for nothing.
        return None
    if current.status in Turn.TERMINAL and current.finished_at and \
            current.finished_at < now - AFTER_FINISH:
        return None
    agent = current.agent if current.agent_id else (
        current.chat_session.agent if current.chat_session_id else None)
    if agent is None:
        return None
    user = current.initiator_user if current.initiator_user_id else None
    if user is not None and not user.is_active:
        return None
    return Grant(turn=current, user=user,
                 tool_globs=canopy_tool_globs(profile(agent, current.capability)),
                 turn_ids=turn_ids)
