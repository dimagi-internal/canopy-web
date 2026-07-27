"""Pydantic v2 schemas for /api/feedback."""
from __future__ import annotations

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
