"""Pydantic v2 schemas for /api/events."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from apps.common.schemas import StrictModel

Level = Literal["info", "warn", "error"]


class EventIn(StrictModel):
    # The `Event` columns' widths.
    source: str = Field(max_length=64)
    kind: str = Field(default="", max_length=64)
    level: Level = "info"
    key: str = Field(default="", max_length=200)
    summary: str = Field(default="", max_length=500)
    payload: dict = {}


class EventBatchIn(StrictModel):
    """Batch on purpose: a runner reports every failure streak it holds in one
    call per tick, atomically, rather than one request per fault."""

    items: list[EventIn]


class EventOut(StrictModel):
    id: int
    workspace: str
    source: str
    kind: str
    level: str
    key: str
    summary: str
    payload: dict
    count: int
    first_seen_at: str
    last_seen_at: str


class EventListOut(StrictModel):
    items: list[EventOut]


class EventRecordOut(StrictModel):
    created: int
    coalesced: int


class SettingChangeOut(StrictModel):
    id: int
    created_at: str
    workspace: str
    actor_email: str
    via: str
    credential: dict[str, Any]
    client: str
    mcp_tool: str
    request_id: str
    operation: str
    method: str
    path: str
    target: str
    summary: str
    request: dict[str, Any]
    changes: list[dict[str, Any]]


class SettingChangeListOut(StrictModel):
    items: list[SettingChangeOut]
