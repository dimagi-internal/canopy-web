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

# What an item asks of you — the one word every client branches on. `reason` is
# only its display text, free to be reworded.
#: A question or decision is put to you: a dialog, a review, a question, a
#: request to join.
ASKING = "asking"
#: Nothing specific is asked, but it is on you: the agent finished and the next
#: message is yours, or a task is parked with you.
YOUR_MOVE = "your_move"
#: Waiting on you, but held behind a runner that is paused or offline: a reply
#: would queue until that box is back.
PARKED = "parked"
#: Not waiting on you: yours and being worked right now.
RUNNING = "running"
#: Not waiting on you: an agent running on its own (auto mode), yours to watch.
RUNNING_AUTO = "running_auto"

STATES = (ASKING, YOUR_MOVE, PARKED, RUNNING, RUNNING_AUTO)
IN_FLIGHT = (RUNNING, RUNNING_AUTO)


@dataclass(frozen=True)
class AttentionRow:
    #: Stable across reads: `<kind>:<id>`. What a hide is recorded against.
    key: str
    kind: str
    #: One of `STATES`.
    state: str
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

    @property
    def in_flight(self) -> bool:
        """Being worked right now: listed apart (`running`) so nothing counts it as an ask."""
        return self.state in IN_FLIGHT
