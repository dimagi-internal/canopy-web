"""Schemas for an agent's state document (`apps/harness/api_agent_state.py`)."""
from __future__ import annotations

import datetime as dt
from typing import Any

from ninja import Schema
from pydantic import ConfigDict, Field


class AgentStateDocumentIn(Schema):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0, description="The version this write is based on; 0 when none exists yet.")
    document: dict[str, Any]


class AgentStateDocumentOut(Schema):
    version: int = Field(description="0 when the agent has no document yet.")
    document: dict[str, Any] | None = None
    updated_at: dt.datetime | None = None


class AgentStateWriteOut(Schema):
    version: int
    updated_at: dt.datetime
