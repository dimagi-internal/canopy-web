from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictModel


class EngagementViewIn(StrictModel):
    """The page a person opened: a project (its numeric `id`) OR a huddle."""

    project: int | None = None
    huddle_id: str = Field(default="", max_length=64)


class EngagementViewOut(StrictModel):
    #: False when this person's view of this object was already counted today (UTC).
    recorded: bool


class EngagementCountOut(StrictModel):
    kind: str
    surface: str
    n: int


class EngagementPersonOut(StrictModel):
    user_id: int
    email: str = ""
    #: Everything they did in the window.
    events: int


class EngagementProposalOut(StrictModel):
    agent: str
    ref: str
    name: str
    status: str
    proposed_at: dt.datetime
    #: Seconds from proposing to the first person backing, commenting or deciding; null when nobody did.
    first_reaction_s: float | None = None
    #: approved / declined / expired, or null while it is still open.
    decision: str | None = None
    decision_s: float | None = None
    backers: int = 0
    comments: int = 0
    #: For a proposed task: its project's ext_id.
    project: str = ""


class EngagementRunsOut(StrictModel):
    announced: int = 0
    confirmed: int = 0
    skipped: int = 0
    expired: int = 0
    #: Median seconds from a run's announcement to its lead's Confirm.
    median_confirm_s: float | None = None


class EngagementSummaryOut(StrictModel):
    since: dt.datetime
    days: int
    counts: list[EngagementCountOut] = Field(default_factory=list)
    people: list[EngagementPersonOut] = Field(default_factory=list)
    proposals: list[EngagementProposalOut] = Field(default_factory=list)
    #: Proposed projects no person reacted to at all.
    unanswered: list[EngagementProposalOut] = Field(default_factory=list)
    #: Tasks a check-in proposed in the window — `ref` is the task's, `project` its project's.
    task_proposals: list[EngagementProposalOut] = Field(default_factory=list)
    unanswered_tasks: list[EngagementProposalOut] = Field(default_factory=list)
    runs: EngagementRunsOut


class AgentRosterRowOut(StrictModel):
    """One agent as a row in the workspace's agent list: what it is, and what it
    is doing — the counts its own pages count with (`health._counts`), and its
    turns as `/api/harness/turns/` would list them."""
    slug: str
    name: str
    description: str
    workspace: str
    avatar_url: str
    email: str
    owner_email: str | None = None
    turn_mode: Literal["manual", "auto"] = "manual"
    url: str
    skills: int = 0
    schedules: int = 0
    active_projects: int = 0
    open_tasks: int = 0
    waiting_tasks: int = 0
    last_turn_at: dt.datetime | None = None
    last_turn_status: str = ""
    turns_7d: int = 0
    failed_7d: int = 0


# ---- the workspace's work: one task by key, and the planning around it ----
# (design 2026-10-24-workspace-tasks-and-projects-design.md)
