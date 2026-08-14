"""Pydantic schemas for the /api/sessions and /api/share surfaces."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import EmailStr, Field

from apps.common.schemas import StrictModel

SessionVisibility = Literal["private", "link"]
MessageRole = Literal["user", "assistant", "system", "tool_use", "tool_result"]


class SessionListItemOut(StrictModel):
    slug: str
    title: str = ""
    project_slug: str | None = None
    visibility: SessionVisibility
    owner_email: EmailStr
    message_count: int = Field(ge=0)
    redaction_count: int = Field(ge=0)
    share_token: str | None = None  # active token, owner only
    is_owner: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class SessionMessageOut(StrictModel):
    turn_index: int
    role: MessageRole
    content: dict
    plaintext: str = ""


class SessionDetailOut(SessionListItemOut):
    messages: list[SessionMessageOut] = []


class SharedSessionOut(StrictModel):
    """Public, read-only payload for /api/share/{token}. No owner identity."""

    title: str = ""
    redaction_count: int = Field(ge=0)
    messages: list[SessionMessageOut] = []


class SessionUploadOut(StrictModel):
    """Result of POST /api/sessions/upload.

    ``owner_email`` is here because a PRIVATE upload has no share token and no
    public page, so the only true statement a client can make about it is *who
    can read it* — and the client cannot know that on its own. Agents upload
    under their own accounts (the canopy uploader resolves its PAT from the repo
    it runs in), so the uploader's identity is frequently not the human's.
    """

    slug: str
    message_count: int = Field(ge=0)
    redaction_count: int = Field(ge=0)
    visibility: SessionVisibility
    owner_email: EmailStr
    share_token: str | None = None
    duplicate: bool = False


class SessionPatchIn(StrictModel):
    title: str | None = Field(default=None, max_length=500)
    project_slug: str | None = None
    visibility: SessionVisibility | None = None


class SessionRotateTokenOut(StrictModel):
    share_token: str


# ---------------------------------------------------------------------------
# Arcs — an ordered group of sessions shared as one page
# ---------------------------------------------------------------------------


class ArcItemIn(StrictModel):
    session_slug: str
    heading: str = Field(default="", max_length=500)


class ArcCreateIn(StrictModel):
    title: str = Field(default="", max_length=500)
    project_slug: str | None = None
    visibility: SessionVisibility = "link"
    items: list[ArcItemIn] = Field(min_length=1)


class ArcPatchIn(StrictModel):
    title: str | None = Field(default=None, max_length=500)
    project_slug: str | None = None
    visibility: SessionVisibility | None = None


class ArcItemOut(StrictModel):
    position: int
    heading: str = ""
    session_slug: str
    session_title: str = ""
    message_count: int = Field(ge=0)
    turn_count: int = Field(ge=0)
    started_at: dt.datetime | None = None
    ended_at: dt.datetime | None = None
    active_seconds: int | None = None


class ArcListItemOut(StrictModel):
    slug: str
    title: str = ""
    project_slug: str | None = None
    visibility: SessionVisibility
    owner_email: EmailStr
    item_count: int = Field(ge=0)
    share_token: str | None = None  # active token, owner only
    is_owner: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class ArcDetailOut(ArcListItemOut):
    items: list[ArcItemOut] = []


class ArcCreateOut(StrictModel):
    slug: str
    visibility: SessionVisibility
    item_count: int = Field(ge=0)
    owner_email: EmailStr
    share_token: str | None = None


# ---------------------------------------------------------------------------
# Public read-only payload — discriminated session | arc
# ---------------------------------------------------------------------------


class SharedSectionOut(StrictModel):
    """One arc section in the public view: a member session's turn-synthesis,
    with the session's properties (when it ran, how many turns)."""

    heading: str = ""
    redaction_count: int = Field(ge=0)
    turn_count: int = Field(ge=0)
    started_at: dt.datetime | None = None
    ended_at: dt.datetime | None = None
    active_seconds: int | None = None
    messages: list[SessionMessageOut] = []


class SharedViewOut(StrictModel):
    """Public, read-only payload for /api/share/{token}, for either a single
    session (``kind="session"``, ``messages`` populated) or an arc
    (``kind="arc"``, ``sections`` populated). No owner identity is exposed.

    For a single session, ``started_at``/``ended_at``/``turn_count`` describe it
    directly; for an arc they span all sections (earliest start, latest end,
    summed turns)."""

    kind: Literal["session", "arc"]
    title: str = ""
    redaction_count: int = Field(ge=0)
    turn_count: int = Field(ge=0)
    started_at: dt.datetime | None = None
    ended_at: dt.datetime | None = None
    active_seconds: int | None = None
    messages: list[SessionMessageOut] = []  # session kind
    sections: list[SharedSectionOut] = []  # arc kind
