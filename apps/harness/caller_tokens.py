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


def mint(turn: Turn, *, scoped: bool = False) -> str:
    """A token for this turn's conversation. Returned once, never stored raw.

    `scoped` is the full-profile variant (`is_caller_scoped`): no capability to
    confine it to, so it lists every tool but still runs each as the asker."""
    raw = PREFIX + secrets.token_urlsafe(32)
    CallerToken.objects.create(token_hash=_hash(raw), turn=turn, chat_session_id=turn.chat_session_id,
                               scoped=scoped, expires_at=timezone.now() + LIFETIME)
    return raw


#: Who holds the agent's whole profile AND may read as the runner's owner would:
#: the people operating it, and canopy itself. Everyone else a `full` decision
#: reaches — a `full:<rule>` grant, a workspace editor, a writer in an admin's
#: session — is a CALLER, and their session reads canopy as themselves.
_OPERATORS = frozenset({"owner", "admin", "system"})


def is_caller_scoped(turn: Turn) -> bool:
    """Whether this FULL-profile turn must reach canopy's MCP as its asker.

    Who-is-asking phase 5 (§9.5, canopy-web#1332). A confined turn already gets
    a caller token; a `full:` turn did not, so its session used the runner's
    PAT — and read across sessions as the runner's owner (2026-10-08, ACE
    answering a `full: [member]` colleague). A turn with no agent (a project
    turn) has no access decision to scope, so it is not scoped."""
    from apps.agents import access

    if turn.capability:
        return False            # confined: has its own token
    agent = turn.agent if turn.agent_id else (
        turn.chat_session.agent if turn.chat_session_id and turn.chat_session.agent_id else None)
    if agent is None:
        return False
    d = access.decide_for_turn(turn, agent)
    return d.access == access.FULL and d.basis not in _OPERATORS


@dataclass
class Grant:
    """What a caller token lets a session do on canopy's MCP, right now."""

    turn: Turn
    user: object | None          # the asker when they are a canopy user; None for a contact
    tool_globs: list[str] = field(default_factory=list)
    #: A scoped token (full profile): every tool exists, each run as the asker.
    all_tools: bool = False
    turn_ids: set[str] = field(default_factory=set)
    #: The CallerToken row, for provenance (which credential created a turn).
    token_id: int | None = None

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
    if current is None:
        return None
    if not current.capability and not tok.scoped:
        # A confined session's conversation should only ever hold confined turns;
        # if the current one is not, this token stands for nothing. (A SCOPED
        # token is the full-profile kind: a full turn is exactly what it serves.)
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
    if not current.capability:
        # Full profile: nothing to list, but still the asker's identity — never
        # the runner's.
        return Grant(turn=current, user=user, all_tools=True, turn_ids=turn_ids, token_id=tok.pk)
    return Grant(turn=current, user=user,
                 tool_globs=canopy_tool_globs(profile(agent, current.capability)),
                 turn_ids=turn_ids, token_id=tok.pk)
