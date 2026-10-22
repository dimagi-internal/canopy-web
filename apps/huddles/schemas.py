"""Pydantic schemas for /api/huddles — a DERIVED view (no models of its own).

The shapes are a contract with the canopy CLI (`canopy huddle await/status/
resume/prompt` read them), so a field renamed here breaks a running huddle."""
from __future__ import annotations

import datetime as dt

from pydantic import Field

from apps.agents.schemas import ProposedProjectOut, ProposedTaskOut, SpawnedProjectOut
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


class HuddleCheckinReadOut(StrictModel):
    """What a check-in run read from its channel before the huddle."""

    messages: int = 0
    first_ts: str = ""
    last_ts: str = ""
    #: The read started after this (the check-in's last read), blank for none.
    since: str = ""


class CheckinProjectOut(StrictModel):
    agent: str
    ref: str
    name: str
    status: str = ""
    #: The project's page (app-relative) — where a check-in's record lives.
    url: str
    workspace: str = ""


class HuddleCheckinOut(StrictModel):
    """The check-in a huddle ran for, when its anchor's `origin_ref` carries a
    check-in run (null otherwise): the header links back to the project."""

    id: int | None = None
    name: str
    project: CheckinProjectOut
    channel_name: str = ""
    lead_email: str = ""
    run_id: int | None = None
    read: HuddleCheckinReadOut | None = None


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
    # The tasks it proposed in its check-in's project, each as the project page shows it.
    tasks: list[ProposedTaskOut] = []
    checkin: HuddleCheckinOut | None = None


# ── Project check-ins (apps/huddles/checkins.py) ─────────────────────────────


class CheckinIn(StrictModel):
    """A new check-in on a project. `lead_email` is the lead's canopy email (a
    member of the project's workspace — and, with a channel, of the channel).
    `channel` is `#name` or a channel id, or blank for none (then nothing is
    read and no announcement is posted: the schedule queues the turn directly).
    `agents` always includes the project's agent (added when missing); every
    other one must be an agent the project's agent can run."""

    name: str = Field(min_length=1, max_length=200)
    topic: str = ""
    lead_email: str
    channel: str = ""
    agents: list[str] = []
    cron: str
    timezone: str = "UTC"
    enabled: bool = True


class CheckinPatch(StrictModel):
    name: str | None = Field(default=None, max_length=200)
    topic: str | None = None
    lead_email: str | None = None
    #: `""` takes the channel off; absent leaves it.
    channel: str | None = None
    agents: list[str] | None = None
    cron: str | None = None
    timezone: str | None = None
    enabled: bool | None = None


class CheckinOut(StrictModel):
    id: int
    name: str
    project: CheckinProjectOut
    workspace: str
    #: The brief the meeting works from (blank: the project's outcome).
    topic: str = ""
    lead_email: str = ""
    lead_slack_id: str = ""
    channel_id: str = ""
    channel_name: str = ""
    #: Agent slugs; the project's agent first.
    agents: list[str] = []
    #: The project's agent — it runs every occurrence.
    agent: str
    #: "solo" (the project's agent works it) or "huddle" (it leads the others).
    format: str
    cron: str = ""
    timezone: str = "UTC"
    next_runs: list[dt.datetime] = []
    schedule_id: int | None = None
    enabled: bool = True
    added_allowance: bool = False
    last_run_status: str = ""
    #: The work group this was before (its slug), for the old /huddles/groups/<slug> link.
    legacy_slug: str = ""
    #: The project's page, where the check-in lives.
    url: str
    can_edit: bool = False
    created_at: dt.datetime
    updated_at: dt.datetime


class CheckinParticipantsOut(StrictModel):
    agents: list[str] = []
    lead_email: str = ""


class CheckinRunOut(StrictModel):
    id: int
    #: The check-in's id — null once the check-in was deleted (the run stays).
    checkin: int | None = None
    checkin_name: str = ""
    project: CheckinProjectOut
    workspace: str
    slot: dt.datetime
    manual: bool = False
    #: awaiting_confirm → reading → running → done; or skipped / expired / failed.
    status: str
    note: str = ""
    #: Who took part, as it was when the run was made.
    participants: CheckinParticipantsOut
    #: Blank: a check-in without a channel (nothing was read).
    channel_id: str = ""
    channel_name: str = ""
    announce_ts: str = ""
    slack_url: str = ""
    confirmed_by_email: str = ""
    confirmed_at: dt.datetime | None = None
    read_messages: int = 0
    read_first_ts: str = ""
    read_last_ts: str = ""
    read_since: dt.datetime | None = None
    #: The channel as read (quoted material). Only on GET /checkins/runs/{id}.
    material: str = ""
    turn_id: str = ""
    huddle_id: str = ""
    #: The huddle's page (app-relative), when the run was a huddle.
    huddle_url: str = ""
    result_url: str = ""
    summary: str = ""
    tasks_proposed: int = 0
    finished_at: dt.datetime | None = None
    created_at: dt.datetime


class CheckinRunTaskOut(StrictModel):
    """A task a run proposed, with its live status and any project it spawned."""

    agent: str
    ref: str
    title: str
    status: str
    url: str
    spawned: list[SpawnedProjectOut] = []


class CheckinTimelineRunOut(CheckinRunOut):
    """One run on the project's timeline: what it proposed, live."""

    tasks: list[CheckinRunTaskOut] = []
    #: Projects its huddle proposed (before check-ins proposed tasks), linked by `huddle_id`.
    projects: list[SpawnedProjectOut] = []


class CheckinRunFinishIn(StrictModel):
    huddle_id: str = ""
    result_url: str = ""
    summary: str = ""
    #: How many tasks it proposed in the project; counted from the tasks filed with `run` when absent.
    tasks_proposed: int | None = Field(default=None, ge=0)
