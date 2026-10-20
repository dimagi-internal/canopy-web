"""The one shape every attention source returns — `AttentionRow`.

A thing waiting on a person, wherever it lives: a chat whose agent needs the
next message, a task asking for a decision or parked on them, a request to join
a workspace. Each owning app builds its own items (`<app>/attention.py`), so the
rule for "is this waiting on you" stays where the thing is; this module only
fixes the shape everyone reads — Home, the API (and so MCP), and any client.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AttentionRow:
    #: Stable across reads: `<kind>:<id>`. What a hide is recorded against.
    key: str
    kind: str
    #: Why it is waiting on you, in a few words ("Approve or decline", "Your move").
    reason: str
    title: str
    workspace: str
    #: App-relative page for it, under its workspace.
    url: str
    #: The last time anything happened on it. A hide lasts until this moves.
    last_activity_at: dt.datetime
    body: str = ""
    agent_slug: str = ""
    project: str = ""
    #: What can be done without leaving the list ("approve", "decline", "reply").
    actions: tuple[str, ...] = ()
    #: Where those actions go: the existing routes' own identifiers.
    target: dict[str, str] = field(default_factory=dict)
    #: Not waiting on you, but yours and in flight right now (a session its agent
    #: is working in). Listed apart — `running` — so nothing counts it as an ask.
    running: bool = False
