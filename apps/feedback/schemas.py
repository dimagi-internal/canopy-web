"""Pydantic v2 schemas for /api/feedback."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from apps.common.schemas import StrictModel

Kind = Literal["comment", "suggestion"]
Channel = Literal["web", "email", "gdoc", "manual", "api"]
State = Literal["new", "triaged", "answered", "declined"]


class FeedbackIn(StrictModel):
    target_kind: str = "narrative"
    target_ref: str
    target_version: int | None = None
    anchor_id: str = ""
    kind: Kind = "comment"
    body: str = ""
    suggested_text: str = ""
    author_name: str = ""
    author_email: str = ""
    channel: Channel = "web"
    source_ref: str = ""


class FeedbackBatchIn(StrictModel):
    """Batch on purpose: a Google Doc has forty comments and an agent ingests
    them in one call, atomically."""

    items: list[FeedbackIn]


class FeedbackOut(StrictModel):
    id: int
    target_kind: str
    target_ref: str
    target_version: int | None
    anchor_id: str
    kind: str
    body: str
    suggested_text: str
    author_name: str
    author_email: str
    channel: str
    source_ref: str
    state: str
    disposition_note: str
    resolved_in_version: int | None
    created_at: str


class FeedbackListOut(StrictModel):
    items: list[FeedbackOut]


class FeedbackIngestOut(StrictModel):
    created: int
    duplicate: int
    empty: int = 0
    """Items skipped for having neither body nor suggested_text."""
    ids: list[int]


class FeedbackResolveIn(StrictModel):
    state: State
    note: str = ""
    resolved_in_version: int | None = None


# --------------------------------------------------- artifact origin + reaction
# Shared by every artifact out-schema (walkthroughs, narrative reviews,
# narratives, storyboards) — board task hal/T76. See apps/feedback/reactions.py
# and apps/harness/artifact_origin.py.


class ArtifactProjectOut(StrictModel):
    """The agent project (``agents.AgentProject``) the artifact's work served."""

    id: int
    agent: str
    ext_id: str
    name: str


class ArtifactSignalsOut(StrictModel):
    """Where an artifact came from, and whether anyone reacted to it."""

    # The canopy chat session / turn it was made from (null when it was made
    # outside one, or before T76 started recording it).
    session_id: uuid.UUID | None = None
    turn_id: uuid.UUID | None = None
    agent_project: ArtifactProjectOut | None = None
    # Feedback rows aimed at it, and the distinct people who left them.
    comment_count: int = 0
    commenter_count: int = 0
    # Distinct signed-in HUMANS who opened it (an agent's own login excluded).
    viewer_count: int = 0
    # When the creator's human (an agent's owner, for an agent upload) last
    # opened it; null when they never have — or before views were recorded.
    owner_viewed_at: dt.datetime | None = None
