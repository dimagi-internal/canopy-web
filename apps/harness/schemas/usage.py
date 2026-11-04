"""The box's usage report schemas."""
from __future__ import annotations

from ninja import Schema
from pydantic import Field


class UsageScopeOut(Schema):
    # The repos canopy knows for this box — the only ones it scans.
    repos: list[str]


class UsageDayIn(Schema):
    day: str = Field(max_length=10)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_creation_tokens: int = Field(default=0, ge=0)
    messages: int = Field(default=0, ge=0)
    model: str = Field(default="", max_length=100)


class UsageSessionIn(Schema):
    """One Claude Code session's whole history, as `canopy_transcript.scan_usage`
    reads it: usage and a little identity, never content."""

    claude_session: str = Field(max_length=64)
    repo: str = Field(default="", max_length=100)
    cwd: str = Field(default="", max_length=500)
    first_at: str = Field(default="", max_length=40)
    last_at: str = Field(default="", max_length=40)
    days: list[UsageDayIn] = Field(default_factory=list, max_length=400)


class UsageReportIn(Schema):
    sessions: list[UsageSessionIn] = Field(default_factory=list, max_length=500)


class UsageReportOut(Schema):
    stored: int
    matched: int
