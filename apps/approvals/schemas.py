"""Schemas for /api/approvals."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictModel


class ApprovalWillRunOut(StrictModel):
    agent: str
    prompt: str


class ApprovalEvidenceOut(StrictModel):
    quote: str
    who: str
    source_url: str = ""
    said_at: dt.datetime | None = None


class ApprovalLinkOut(StrictModel):
    label: str
    url: str


class ApprovalDecidedOut(StrictModel):
    decision: Literal["approve", "decline", "other"]
    by: str
    via: str
    at: dt.datetime | None = None
    comment: str = ""


class ApprovalCardOut(StrictModel):
    key: str
    title: str
    state: Literal["open", "approved", "declined", "closed"]
    workspace: str
    url: str
    agent_slug: str
    agent_name: str
    filed_by: str
    summary: str
    evidence: list[ApprovalEvidenceOut]
    rationale: str
    plan: list[str]
    will_run: list[ApprovalWillRunOut]
    if_declined: str
    links: list[ApprovalLinkOut]
    source_url: str
    priority: int
    confidence: str
    created_at: dt.datetime
    sent_to: list[str]
    decided: ApprovalDecidedOut | None = None
    can_decide: bool


class ApprovalDecideIn(StrictModel):
    decision: Literal["approve", "decline"]
    comment: str = Field(default="", max_length=2000)
