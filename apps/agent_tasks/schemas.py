"""Pydantic v2 schemas for the agent-task routes."""
from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import Field

from apps.common.schemas import StrictModel
from apps.harness.schemas import ParentIn, Routing

AgentTaskCallStatus = Literal["queued", "running", "done", "failed", "cancelled"]


class AgentTaskCallIn(StrictModel):
    # The agent's slug.
    agent: str = Field(min_length=1, max_length=64)
    # What kind of task, e.g. `gmail-triage.review`; names a skill when it matches one.
    name: str = Field(min_length=1, max_length=120)
    instructions: str = Field("", max_length=20_000)
    # Any JSON, at most 1 MiB serialized (413 above it).
    input: Any
    # A JSON Schema object (checked to be one: 422); the result must validate against it.
    output_schema: dict[str, Any]
    routing: Routing = "prefer_local"
    timeout_s: int = Field(900, ge=30, le=3600)
    parent: ParentIn | None = None


class AgentTaskResultIn(StrictModel):
    result: Any


class AgentTaskCallOut(StrictModel):
    id: str
    agent: str
    name: str
    status: AgentTaskCallStatus
    # Set once `status` is done.
    result: Any = None
    # Why it failed or was cancelled.
    error: str = ""
    # Follow-up turns sent because a turn ended without a result.
    nudges: int
    # The latest turn working it.
    turn_id: str | None = None
    timeout_s: int
    deadline_at: dt.datetime
    created_at: dt.datetime
    finished_at: dt.datetime | None = None
    instructions: str = ""
    output_schema: dict[str, Any]
    # Only for the agent's own login, which works it: null for the creator (who sent
    # it) and the agent's admins, so a long-poll does not carry a megabyte back.
    input: Any = None
