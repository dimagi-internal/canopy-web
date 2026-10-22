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


class AlertActionOut(Schema):
    id: str
    label: str
    closes: bool
    # Set → a link: the fix is done on that page, never acted on here.
    href: str = ""


class AlertOut(Schema):
    id: int
    kind: str
    severity: Literal["blocking", "action", "setup"]
    title: str
    body: str = ""
    url: str = ""
    workspace: str
    # The top of `workspace`'s tree, like an attention row's.
    organisation: str
    scopes: list[str]
    status: Literal["open", "resolved", "dismissed"]
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime
    occurrences: int
    closed_by_email: str = ""
    closed_at: dt.datetime | None = None
    # What THIS viewer may do now; empty for a closed alert.
    actions: list[AlertActionOut]
    # Display line of the latest `notified` event ("Ann asked Bob at 14:02"); "" if none.
    last_notify: str = ""


class AlertEventOut(Schema):
    at: dt.datetime
    kind: str
    actor_email: str = ""
    action_id: str = ""
    payload: dict


class AlertActIn(Schema):
    action: str
