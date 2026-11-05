"""Pydantic schemas for the canopy.origin issue-record API."""
from __future__ import annotations

import datetime as dt

from pydantic import Field

from apps.common.schemas import StrictModel


class OriginIssueIn(StrictModel):
    """Upsert payload — the canopy.origin record minus envelope fields (`schema`/`issue` are derived)."""

    repo: str = Field(min_length=1, max_length=200)
    number: int = Field(ge=1, le=2**31 - 1)  # an IntegerField
    title: str = Field(min_length=1, max_length=300)
    # Bounded to the model's column widths: past them Postgres raises a
    # DataError the API can only answer with a 500.
    source: str = Field(default="hal-architect", max_length=100)
    agent: str = Field(default="hal", max_length=100)
    skill: str = Field(default="architect", max_length=100)
    initiative: str = Field(default="", max_length=200)
    ledger: str = Field(default="", max_length=500)
    created: str = Field(default="", max_length=40)
    disposition: str = Field(default="route", max_length=20)
    confidence: str = Field(default="medium", max_length=20)
    mandate: str = ""
    done_when: str = ""
    intent: str = ""
    evidence: list[dict] = Field(default_factory=list)   # pointers only — claim → session path
    corpus: dict = Field(default_factory=dict)           # {sessions_scanned, cross_user, drilled:[paths]}


class OriginIssueOut(StrictModel):
    id: int
    repo: str
    number: int
    title: str
    source: str
    agent: str
    skill: str
    initiative: str
    ledger: str
    created: str
    disposition: str
    confidence: str
    mandate: str
    done_when: str
    intent: str
    evidence: list[dict]
    corpus: dict
    created_at: dt.datetime
    updated_at: dt.datetime
