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


# ── Work groups (apps/huddles/groups.py) ─────────────────────────────────────


class WorkGroupIn(StrictModel):
    """A new work group. `channel` is `#name` or a channel id; `lead_email` the
    lead's canopy email (they must be in the workspace AND the channel);
    `leader_agent` is required with more than one agent and must be able to run
    every other agent (its login an owner or admin of each)."""

    slug: str
    name: str
    topic: str = ""
    channel: str
    lead_email: str
    agents: list[str]
    leader_agent: str = ""
    cron: str
    timezone: str = "UTC"
    enabled: bool = True


class WorkGroupPatch(StrictModel):
    name: str | None = None
    topic: str | None = None
    channel: str | None = None
    lead_email: str | None = None
    agents: list[str] | None = None
    leader_agent: str | None = None
    cron: str | None = None
    timezone: str | None = None
    enabled: bool | None = None


class WorkGroupProjectOut(StrictModel):
    agent: str
    ref: str
    name: str
    status: str
    url: str


class WorkGroupOut(StrictModel):
    slug: str
    workspace: str
    name: str
    topic: str = ""
    lead_email: str = ""
    lead_slack_id: str = ""
    channel_id: str
    channel_name: str = ""
    agents: list[str] = []
    leader_agent: str
    #: "single" (one agent works the group's project) or "multi" (the leader runs a huddle).
    mode: str
    cron: str = ""
    timezone: str = "UTC"
    next_runs: list[dt.datetime] = []
    schedule_id: int | None = None
    project: WorkGroupProjectOut | None = None
    enabled: bool = True
    last_run_status: str = ""
    url: str
    can_edit: bool = False
    created_at: dt.datetime
    updated_at: dt.datetime


class WorkGroupRunOut(StrictModel):
    id: int
    group: str
    group_name: str
    workspace: str
    slot: dt.datetime
    manual: bool = False
    #: awaiting_confirm → reading → running → done; or skipped / expired / failed.
    status: str
    note: str = ""
    announce_ts: str = ""
    slack_url: str = ""
    confirmed_by_email: str = ""
    confirmed_at: dt.datetime | None = None
    read_messages: int = 0
    read_first_ts: str = ""
    read_last_ts: str = ""
    read_since: dt.datetime | None = None
    #: The channel as read (quoted material). Only on GET /huddle-groups/runs/{id}.
    material: str = ""
    turn_id: str = ""
    huddle_id: str = ""
    result_url: str = ""
    summary: str = ""
    finished_at: dt.datetime | None = None
    created_at: dt.datetime


class WorkGroupRunFinishIn(StrictModel):
    huddle_id: str = ""
    result_url: str = ""
    summary: str = ""
