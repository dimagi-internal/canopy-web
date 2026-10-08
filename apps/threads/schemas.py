"""Pydantic schemas for /api/threads.

The shapes are a contract with the canopy CLI (`canopy thread open/say/run/show`
read and write them), so a field renamed here breaks a running thread."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictModel


class ParticipantIn(StrictModel):
    agent: str = Field(min_length=1, max_length=80)
    role: str = Field(default="", max_length=120)


class ThreadIn(StrictModel):
    kind: str = Field(min_length=1, max_length=40)
    purpose: str = Field(min_length=1, max_length=300)
    participants: list[ParticipantIn]
    moderator: str = Field(min_length=1, max_length=80)
    parent: dict = {}
    context: str = Field(default="", max_length=20_000)
    max_messages: int = Field(default=4, ge=1, le=12)
    deadline_minutes: int = Field(default=90, ge=1, le=7 * 24 * 60)


class ThreadCloseIn(StrictModel):
    status: Literal["settled", "out_of_budget", "timed_out", "cancelled"]
    outcome: dict = {}


class ThreadMessageOut(StrictModel):
    n: int
    speaker: str
    turn_id: str
    status: str
    created_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    content_hidden: bool = False
    prompt: str = ""
    block: dict | None = None
    reply_source: str = "none"
    reply_error: str = ""


class ThreadOut(StrictModel):
    id: str
    kind: str
    purpose: str
    participants: list[dict] = []
    moderator: str
    parent: dict = {}
    context: str = ""
    max_messages: int
    messages_used: int = 0
    deadline_at: dt.datetime
    status: str
    outcome: dict = {}
    created_at: dt.datetime
    closed_at: dt.datetime | None = None
    messages: list[ThreadMessageOut] = []
