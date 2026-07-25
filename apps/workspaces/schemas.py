"""Pydantic schemas for the /api/workspaces surface."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictModel

Role = Literal["owner", "editor", "viewer"]


class WorkspaceCreateIn(StrictModel):
    slug: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    auto_join_domains: list[str] = Field(default_factory=list)


class WorkspaceOut(StrictModel):
    slug: str
    display_name: str
    auto_join_domains: list[str]
    role: str  # the requesting user's role in this workspace
    created_at: dt.datetime


class MemberOut(StrictModel):
    user_id: int
    email: str
    role: str
    joined_at: dt.datetime


class InviteCreateIn(StrictModel):
    email: str = Field(min_length=3, max_length=200)
    role: Role = "editor"


class InviteOut(StrictModel):
    id: int
    email: str
    role: str
    token: str
    expires_at: dt.datetime
    accepted_at: dt.datetime | None = None
    revoked_at: dt.datetime | None = None


InviteStatus = Literal["pending", "expired", "revoked", "accepted"]


class InvitePreviewOut(StrictModel):
    """Pre-auth invite preview — deliberately minimal disclosure. For any
    non-pending status, workspace_slug/workspace_display_name/role stay None:
    someone holding a dead token (forwarded, pasted into Slack, ...) learns
    only that it's dead, never which tenant it pointed at."""

    status: InviteStatus
    email_hint: str
    workspace_slug: str | None = None
    workspace_display_name: str | None = None
    role: str | None = None
