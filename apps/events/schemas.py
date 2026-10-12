"""Pydantic v2 schemas for /api/events."""
from __future__ import annotations

from typing import Any, Literal

from apps.common.schemas import StrictModel

Level = Literal["info", "warn", "error"]


class EventIn(StrictModel):
    source: str
    kind: str = ""
    level: Level = "info"
    key: str = ""
    summary: str = ""
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


class McpCallOut(StrictModel):
    id: int
    created_at: str
    workspace: str
    user_email: str
    tool: str
    args_summary: str
    ok: bool
    error: str


class McpCallListOut(StrictModel):
    items: list[McpCallOut]


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
