import datetime as dt

from ninja import Field, Schema


class AttentionRowOut(Schema):
    key: str
    kind: str
    reason: str
    title: str
    body: str = ""
    workspace: str
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
    # Set aside by this person; each comes back when something new happens on it.
    let_go: list[AttentionRowOut]


class LetGoIn(Schema):
    keys: list[str] = Field(min_length=1, max_length=500)
    # Set aside for this many days instead of until something new happens.
    days: int | None = Field(default=None, ge=1, le=90)


class LetGoOut(Schema):
    count: int
