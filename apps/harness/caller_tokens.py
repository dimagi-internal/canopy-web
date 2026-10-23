"""Mint and resolve CallerTokens — see `models.CallerToken` for why they exist."""
from __future__ import annotations

import datetime as dt
import fnmatch
import secrets
from dataclasses import dataclass, field

from django.utils import timezone

from apps.agents import sensitive_data
from apps.common.token_hash import hash_token

from . import sensitivity
from .models import CallerToken, Turn

PREFIX = "cct_"
#: A conversation's token outlives turns but not forever.
LIFETIME = dt.timedelta(days=7)
#: A finished turn's session may still be settling (the reply streaming back).
AFTER_FINISH = dt.timedelta(minutes=10)
#: The MCP server name as Claude Code mounts it — `mcp__<mount>canopy-web__<tool>`.
_SERVER = "canopy-web__"


def _hash(raw: str) -> str:
    return hash_token(raw)


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


def _project_repo(turn: Turn) -> str:
    """The repo an agentless turn drives — a project dispatch's own, or a project
    chat's (its session's) — else "" (an agent's turn, or a chat with neither)."""
    if turn.acting_agent is not None:
        return ""
    if turn.project:
        return turn.project
    cs = turn.chat_session if turn.chat_session_id else None
    return cs.project if cs is not None else ""


def _project_asker(turn: Turn):
    """The person a PROJECT turn acts as on canopy: its verified, active human
    initiator — else None (a schedule, a drill, a contact, anyone unproven).

    A project has no agent to act as, so its session acts as whoever asked,
    exactly as a colleague's full turn does: what they can see is what it can see
    (access.md → "Projects run as the person who asked")."""
    from .caller_context import is_verified

    if not _project_repo(turn) or not turn.initiator_user_id:
        return None
    user = turn.initiator_user
    if not user.is_active or not is_verified(turn):
        return None
    return user


def is_caller_scoped(turn: Turn) -> bool:
    """Whether this FULL-profile turn must reach canopy's MCP as its asker.

    Who-is-asking phase 5 (§9.5, canopy-web#1332). A confined turn already gets
    a caller token; a `full:` turn did not, so its session used the runner's
    PAT — and read across sessions as the runner's owner (2026-10-08, ACE
    answering a `full: [member]` colleague). A turn with no agent (a project
    turn or project chat) has no agent to act as, so it acts as its verified
    asker (`_project_asker`); with no such person it gets no token at all."""
    from apps.agents import access

    if turn.capability:
        return False            # confined: has its own token
    agent = turn.acting_agent
    if agent is None:
        return _project_asker(turn) is not None
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
    #: The turn holds a host's sensitive data (`_sensitive_grant`).
    sensitive: bool = False

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
    if not current.capability and sensitivity.is_sensitive(current):
        return None             # a sensitive turn runs confined, or its token is nothing
    agent = current.acting_agent
    if agent is None:
        # A project's session acts as its CURRENT asker, re-checked on every call
        # (a later turn in the chat by someone unverified stands for no one).
        user = _project_asker(current) if tok.scoped else None
        if user is None:
            return None
        return Grant(turn=current, user=user, all_tools=True, turn_ids=turn_ids, token_id=tok.pk)
    user = current.initiator_user if current.initiator_user_id else None
    if user is not None and not user.is_active:
        return None
    if not current.capability:
        # Full profile: nothing to list, but still the asker's identity — never
        # the runner's.
        return Grant(turn=current, user=user, all_tools=True, turn_ids=turn_ids, token_id=tok.pk)
    globs = canopy_tool_globs(profile(agent, current.capability))
    if sensitivity.is_sensitive(current):
        return _sensitive_grant(current, agent, globs, user=user, turn_ids=turn_ids,
                                token_id=tok.pk)
    return Grant(turn=current, user=user, tool_globs=globs, turn_ids=turn_ids, token_id=tok.pk)


def _sensitive_grant(current: Turn, agent, globs: list[str], **kw) -> Grant | None:
    """A sensitive turn's grant: at most the canopy tools a `data: sensitive`
    capability may hold (`sensitive_data.SENSITIVE_CANOPY_TOOLS`), and only while
    its capability still says `data: sensitive`. Asked on every call, so an
    interface edited mid-turn — the switch turned off, a tool added — narrows the
    token at once rather than widening it: a sensitive turn never reaches a tool
    that would write its data somewhere else, whatever the interface says now."""
    if not sensitive_data.handles_sensitive(agent, current.capability):
        return None
    allowed = sorted(name for name in sensitive_data.SENSITIVE_CANOPY_TOOLS
                     if any(fnmatch.fnmatchcase(name, g) for g in globs))
    return Grant(turn=current, tool_globs=allowed, sensitive=True, **kw)
