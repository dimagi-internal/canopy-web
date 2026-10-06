"""Pydantic schemas for the /api/shareouts surface."""
from __future__ import annotations

import datetime as dt

from pydantic import Field

from apps.common.schemas import StrictModel


class ShareoutLink(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=500)


class ShareoutPR(StrictModel):
    number: int | None = None
    title: str = ""
    url: str = ""
    state: str = ""


#: A repo slug: letters, digits, `-`, `_` and `.` (repo names carry dots), not
#: starting with a separator — or "" (treated like null: the roll-up). Any
#: well-formed slug is accepted; there is no project registry to check it against.
PROJECT_SLUG_PATTERN = r"^([A-Za-z0-9][A-Za-z0-9._-]*)?$"


class ShareoutIn(StrictModel):
    """One briefing in a POST batch. `project_slug` omitted/null = roll-up."""

    project_slug: str | None = Field(default=None, max_length=200, pattern=PROJECT_SLUG_PATTERN)
    period_start: dt.datetime
    period_end: dt.datetime
    title: str = Field(min_length=1, max_length=200)
    summary: str = ""
    content: str = Field(min_length=1)
    links: list[ShareoutLink] = Field(default_factory=list)
    all_prs: list[ShareoutPR] = Field(default_factory=list)
    author: str = Field(default="", max_length=100)
    # The agent that produced this on the author's behalf (slug), or "" for a
    # human run. Optional so a human/legacy post omits it entirely.
    produced_by_agent: str = Field(default="", max_length=80)
    source: str = Field(min_length=1, max_length=100)


class ShareoutBatchIn(StrictModel):
    shareouts: list[ShareoutIn] = Field(min_length=1)


class ShareoutBatchOut(StrictModel):
    created: int
    replaced: int


class ShareoutsClearIn(StrictModel):
    """Body of POST /api/shareouts/clear/. All optional, AND-combined. An empty
    body clears every shareout the caller may clear: their own, plus every
    shareout in a workspace they own."""

    source: str | None = None
    project: str | None = None  # project slug
    date_from: dt.date | None = None
    date_to: dt.date | None = None


class ShareoutsClearOut(StrictModel):
    cleared: int


class ShareoutOut(StrictModel):
    id: int
    project_slug: str | None = None
    period_start: dt.datetime
    period_end: dt.datetime
    title: str
    summary: str
    content: str
    links: list[ShareoutLink] = Field(default_factory=list)
    all_prs: list[ShareoutPR] = Field(default_factory=list)
    author: str
    produced_by_agent: str = ""
    source: str
    created_at: dt.datetime
