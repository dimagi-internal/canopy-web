"""Which chat an MCP request is acting for — read from its chat key.

The caller's bearer token says WHO is calling (an agent's own login, usually).
The `X-Canopy-Chat-Key` header, when present, says which CHAT that session is
driving: canopy minted it when the runner claimed the chat's turn and gave it
only to that session (`canopy_sessions.ChatKey`). Page tools answer about that
one chat when it is present — narrower than "every page this user may see",
and correct for an agent, whose login is shared by every chat it is in.
"""
from __future__ import annotations

from fastmcp.server.dependencies import get_http_headers


def current_chat_session():
    """The chat this request's key names, or None (no header, or a bad key).
    Synchronous: call it through `sync_to_async`, it reads the database."""
    from apps.canopy_sessions import chat_keys

    raw = get_http_headers().get(chat_keys.HEADER.lower())
    return chat_keys.resolve(raw)
