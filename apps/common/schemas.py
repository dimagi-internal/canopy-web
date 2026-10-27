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

    # Your own user id: what your member page in each workspace is addressed by
    # (/w/<ws>/members/<user_id>).
    user_id: int | None = None
    email: EmailStr
    name: str
    avatar_url: str
    can_create_workspace: bool = Field(
        default=False,
        description=(
            "Whether this caller may POST /api/workspaces/. False for an "
            "invite-admitted user holding no membership — see the F1 finding "
            "in apps.workspaces.services.can_create_workspace. The first-run "
            "screen reads this so it never offers a button that 403s."
        ),
    )
    memory_enabled: bool = Field(
        default=False,
        description=(
            "Is memory (HCP) on for you yet (settings.HCP_PEOPLE)? False hides "
            "every memory surface: the Your memory page and its links, the "
            "conversation switches and grant prompts."
        ),
    )
    memory_paused: bool = Field(
        default=False,
        description=(
            "Memory is not on for you, but canopy holds some of yours: the Your "
            "memory page still shows it, to download or delete."
        ),
    )


class PresencePreferenceOut(StrictModel):
    """Response for GET/PATCH /api/me/presence-preference/."""

    show_presence: bool


class PresencePreferenceIn(StrictModel):
    """Request body for PATCH /api/me/presence-preference/."""

    show_presence: bool


RunnerAppPage = Literal["supervisor", "ide"]


class RunnerAppChoice(StrictModel):
    value: RunnerAppPage
    label: str


class RunnerAppPreferenceOut(StrictModel):
    """Response for GET/PATCH /api/me/runner-app/."""

    opens_on: RunnerAppPage = Field(
        description="The page the Canopy Runner desktop app opens on."
    )
    choices: list[RunnerAppChoice] = Field(
        description="Every page it can open on, in menu order."
    )


class RunnerAppPreferenceIn(StrictModel):
    """Request body for PATCH /api/me/runner-app/."""

    opens_on: RunnerAppPage


class SessionWorkspaceChoice(StrictModel):
    slug: str
    display_name: str


class SessionWorkspacePreferenceOut(StrictModel):
    """Response for GET/PATCH /api/me/session-workspace/."""

    workspace: str | None = Field(
        default=None,
        description="The workspace you chose for your own emdash sessions, or null if unset.",
    )
    effective: str | None = Field(
        default=None,
        description=(
            "The workspace your new sessions are filed in right now: `workspace` while "
            "you can still edit there, else null (each runner's own workspace is used)."
        ),
    )
    choices: list[SessionWorkspaceChoice] = Field(
        default_factory=list,
        description="Workspaces you may file sessions in, or move one to (editor or better).",
    )


class SessionWorkspacePreferenceIn(StrictModel):
    """Request body for PATCH /api/me/session-workspace/. A blank or null
    `workspace` clears the default."""

    workspace: str | None = None


# --- Health ------------------------------------------------------------


class HealthOut(StrictModel):
    status: str
