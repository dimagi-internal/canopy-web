"""Schemas for /api/approvals."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictModel

Status = Literal["pending", "done", "failed", "declined", "cancelled", "expired"]


class ApprovalEvidenceIn(StrictModel):
    quote: str = Field(min_length=1, max_length=1500)
    who: str = Field(default="", max_length=200)
    source_url: str = Field(default="", max_length=500)


class ApprovalFileIn(StrictModel):
    kind: str = Field(description="One of GET /api/approvals/kinds, e.g. `workspace.member.add`.")
    params: dict = Field(description="That kind's typed params (see its `params_schema`).")
    reason: str = Field(default="", max_length=4000,
                        description="Why you are asking, in your own words. Shown as yours, apart from the preview.")
    evidence: list[ApprovalEvidenceIn] = Field(default_factory=list, max_length=5,
                                               description="What a person actually said, verbatim.")
    source_url: str = Field(default="", max_length=500)
    for_email: str = Field(default="", max_length=254,
                           description="Send it to this one person (they must be able to approve it).")
    expires_in_hours: int | None = Field(default=None, ge=1, le=720)
    idempotency_key: str = Field(default="", max_length=128,
                                 description="A replay with the same key returns the original request.")


class ApprovalDecideIn(StrictModel):
    decision: Literal["approve", "decline"]
    comment: str = Field(default="", max_length=2000)


class ApprovalEvidenceOut(StrictModel):
    quote: str
    who: str
    source_url: str


class ApprovalDecidedOut(StrictModel):
    by: str
    via: str
    at: dt.datetime | None = None
    comment: str


class ApprovalHistoryOut(StrictModel):
    kind: str
    by: str
    at: dt.datetime


class ApprovalCardOut(StrictModel):
    id: int
    key: str
    kind: str
    kind_label: str
    needs: str
    title: str
    summary: str
    status: Status
    workspace: str
    url: str
    requested_by: str
    reason: str
    evidence: list[ApprovalEvidenceOut]
    plan: list[str]
    if_declined: str
    source_url: str
    created_at: dt.datetime
    expires_at: dt.datetime | None = None
    sent_to: list[str]
    decided: ApprovalDecidedOut | None = None
    outcome: str
    result: dict
    error: str
    can_decide: bool
    history: list[ApprovalHistoryOut]


class ApprovalKindOut(StrictModel):
    key: str
    label: str
    needs: str
    does: str
    params_schema: dict
