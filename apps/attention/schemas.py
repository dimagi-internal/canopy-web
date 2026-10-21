import datetime as dt
from typing import Literal

from ninja import Field, Schema

# Mirrors `types.STATES`; `tests/test_attention.py` keeps the two equal.
State = Literal["asking", "your_move", "parked", "running", "running_auto"]


class AttentionRowOut(Schema):
    key: str
    kind: str
    # What it asks of you — branch on this, never on `reason`, which is display text.
    state: State
    reason: str
    title: str
    body: str = ""
    workspace: str
    # The top of `workspace`'s tree: a user-level read spans organisations and
    # labels each row with its own.
    organisation: str = ""
    agent_slug: str = ""
    project: str = ""
    url: str
    last_activity_at: dt.datetime
    actions: list[str] = Field(default_factory=list)
    target: dict[str, str] = Field(default_factory=dict)


class AttentionOut(Schema):
    # Something happened on it in the last few days, newest first.
    now: list[AttentionRowOut]
    # Still waiting, but nothing has happened on it for a while.
    quiet: list[AttentionRowOut]
    # Hidden by this person; each comes back when something new happens on it.
    hidden: list[AttentionRowOut]
    # Not waiting on you: yours and in flight right now (a session being worked).
    running: list[AttentionRowOut] = Field(default_factory=list)


class HideIn(Schema):
    keys: list[str] = Field(min_length=1, max_length=500)
    # Hide for this many days instead of until something new happens.
    days: int | None = Field(default=None, ge=1, le=90)


class HideOut(Schema):
    count: int
