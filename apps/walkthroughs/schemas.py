"""Pydantic schemas for the /api/walkthroughs surface.

Mirror the field set from apps/walkthroughs/serializers.py — the
walkthroughs app shipped in PR #40 (2026-05-26) with DRF serializers
that this replaces.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import EmailStr, Field

from apps.common.schemas import StrictModel
from apps.feedback.schemas import ArtifactSignalsOut

WalkthroughKind = Literal["html", "video"]
WalkthroughVisibility = Literal["private", "link"]
WalkthroughLinkKind = Literal["narrative", "companion", "reference"]


class WalkthroughLink(StrictModel):
    """One companion link rendered on the viewer page.

    ``kind`` drives which section it lands in:
      narrative  — the design story / spec that generated this walkthrough
      companion  — the sibling artifact (still-frame deck ↔ video)
      reference  — a destination shown in the demo, for the viewer to explore
    """

    label: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    kind: WalkthroughLinkKind = "reference"


class WalkthroughListItemOut(ArtifactSignalsOut):
    id: uuid.UUID
    title: str
    description: str = ""
    kind: WalkthroughKind
    project_slug: str | None = None
    visibility: WalkthroughVisibility
    owner_email: EmailStr
    size_bytes: int = Field(ge=0)
    duration_sec: int | None = None
    # DDD-run grouping (null for one-off uploads).
    run_id: str | None = None
    narrative_slug: str | None = None
    role: str | None = None
    # The recorded-narrative cut this video is (the recipe's cuts[].id); "" otherwise.
    cut_id: str = ""
    created_at: dt.datetime
    updated_at: dt.datetime


class WalkthroughDetailOut(WalkthroughListItemOut):
    """Detail view adds content_type, is_owner, and the owner-only share_url."""

    content_type: str
    is_owner: bool
    links: list[WalkthroughLink] = []
    # Absolute tokened public URL (…/w/<ws>/walkthrough/<id>?t=<token>). Present
    # only for the owner of a public (visibility=link) walkthrough; never expose
    # the raw token as its own field.
    share_url: str | None = None
    # The workspace it lives in — where its page is (/w/<workspace>/walkthrough/
    # <id>). A reader holding the link already has it; a flat link resolves it.
    workspace: str | None = None


class WalkthroughUploadIn(StrictModel):
    """Form-encoded body of POST /walkthroughs/ (alongside the multipart file).

    Used to validate the non-file form fields; the actual file comes
    through Ninja's UploadedFile primitive on the handler.
    """

    title: str = ""
    kind: WalkthroughKind
    project_slug: str = ""
    description: str = ""
    visibility: WalkthroughVisibility = "private"
    links: list[WalkthroughLink] = []
    run_id: str = ""
    narrative_slug: str = ""
    role: str = ""
    cut_id: str = ""
    cut_scene_ids: str = ""


class WalkthroughPatchIn(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    project_slug: str | None = None  # may be set to null to detach
    visibility: WalkthroughVisibility | None = None
    links: list[WalkthroughLink] | None = None


