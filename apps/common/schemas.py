"""Cross-cutting Pydantic schemas reused across canopy-web apps.

Conventions:
- Output schemas end in `Out`, input in `In`, patches in `Patch`.
- IDs that are slugs use `str`; numeric PKs use `int`.
- All datetimes are timezone-aware ISO-8601 (Pydantic v2 default).
- Optional fields use `T | None = None`; required fields have no default.
"""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",  # request bodies reject unknown fields
        from_attributes=True,  # allow ORM-instance hydration
        str_strip_whitespace=True,
    )


class TimestampMixin(BaseModel):
    created_at: dt.datetime
    updated_at: dt.datetime | None = None


class UserRefOut(StrictModel):
    """Minimal user reference for embedding in other responses."""

    id: int
    email: EmailStr
    display_name: str | None = None


class MeOut(StrictModel):
    """Response for /api/me/."""

    email: EmailStr
    name: str
    avatar_url: str


class PresencePreferenceOut(StrictModel):
    """Response for GET/PATCH /api/me/presence-preference/."""

    show_presence: bool


class PresencePreferenceIn(StrictModel):
    """Request body for PATCH /api/me/presence-preference/."""

    show_presence: bool


# --- Health ------------------------------------------------------------


class HealthOut(StrictModel):
    status: str


# --- AI backend (/api/ai/) --------------------------------------------

AiBackend = Literal["api", "cli"]


class AiStatusOut(StrictModel):
    backend: AiBackend
    authenticated: bool
    detail: str | None = None


class AiSwitchIn(StrictModel):
    backend: AiBackend


class AiSwitchOut(StrictModel):
    backend: AiBackend
    detail: str | None = None


class AiAuthStartOut(StrictModel):
    auth_url: str
    state: str


class AiAuthCompleteIn(StrictModel):
    code: str = Field(min_length=1)


class AiAuthCompleteOut(StrictModel):
    ok: bool
    detail: str | None = None


class AiAuthPollOut(StrictModel):
    state: Literal["idle", "pending", "ok", "error"]
    detail: str | None = None
