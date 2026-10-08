"""Pydantic schemas for /api/huddles — a DERIVED view (no models of its own).

The shapes are a contract with the canopy CLI (`canopy huddle await/status/
resume/prompt` read them), so a field renamed here breaks a running huddle."""
from __future__ import annotations

import datetime as dt

from apps.common.schemas import StrictModel


class HuddleSummaryOut(StrictModel):
    id: str
    type: str = ""
    team: str = ""
    leader: str = ""
    members: list[str] = []
    anchor_turn_id: str
    created_at: dt.datetime
    finished: bool = False
    outcome_count: int = 0
    rounds_dispatched: int = 0


class HuddleCellOut(StrictModel):
    member: str
    round: int
    attempt: int = 1
    turn_id: str
    status: str
    created_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    content_hidden: bool = False
    prompt: str = ""
    block: dict | None = None
    reply_source: str = "none"
    reply_error: str = ""
    has_transcript: bool = False


class HuddleOutputOut(StrictModel):
    agent: str
    task_id: int
    ext_id: str
    title: str
    status: str
    assigned: str = ""
    project: str = ""
    url: str
    # The task's single next step, as its agent wrote it — where a task that
    # reads "in progress" says it is actually stuck ("BLOCKED on creds …").
    next_action: str = ""
    updated_at: dt.datetime | None = None


class HuddleOut(HuddleSummaryOut):
    summary: str = ""
    # The numbered priorities the huddle worked toward ("" for a huddle planned
    # without one); proposals name these by number.
    priorities_brief: str = ""
    deadline_at: dt.datetime | None = None
    cells: list[HuddleCellOut] = []
    outputs: list[HuddleOutputOut] = []
