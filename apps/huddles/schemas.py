"""Pydantic schemas for /api/huddles — a DERIVED view (no models of its own).

The shapes are a contract with the canopy CLI (`canopy huddle await/status/
resume/prompt` read them), so a field renamed here breaks a running huddle."""
from __future__ import annotations

import datetime as dt

from apps.agents.schemas import ProposedProjectOut
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


class HuddleGroupReadOut(StrictModel):
    """What a work-group run read from its channel before the huddle."""

    messages: int = 0
    first_ts: str = ""
    last_ts: str = ""
    #: The read started after this (the group's last read), blank for a first read.
    since: str = ""


class HuddleGroupOut(StrictModel):
    """The work group a huddle ran for, when its anchor's `origin_ref` carries a
    group run (filled by the work-groups layer; null otherwise)."""

    slug: str
    name: str
    channel_name: str = ""
    lead_email: str = ""
    run_id: int | None = None
    read: HuddleGroupReadOut | None = None


class HuddleOut(HuddleSummaryOut):
    summary: str = ""
    # The numbered priorities the huddle worked toward ("" for a huddle planned
    # without one); proposals name these by number.
    priorities_brief: str = ""
    deadline_at: dt.datetime | None = None
    cells: list[HuddleCellOut] = []
    # A huddle filed before proposed projects existed: its outcomes as loose tasks.
    outputs: list[HuddleOutputOut] = []
    # The projects it proposed (their `source_url` is this huddle's page), each as
    # the project page shows it — people back, comment and decide right here.
    projects: list[ProposedProjectOut] = []
    group: HuddleGroupOut | None = None
