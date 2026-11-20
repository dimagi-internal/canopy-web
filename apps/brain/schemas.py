"""Pydantic schemas for /api/me/learning."""
from __future__ import annotations

import datetime as dt
import uuid

from pydantic import ConfigDict, Field

from apps.common.schemas import StrictModel


class LearningClassOut(StrictModel):
    name: str = Field(description="individual | memory | project | system.")
    learns_about: str
    summary: str = Field(description="What agreeing means, in the words the person is shown.")
    seen_by: str = Field(description="Who sees what it produces.")
    on: bool


class LearningOut(StrictModel):
    on: bool = Field(description="Whether you have agreed to any class.")
    since: dt.datetime | None = None
    classes: list[LearningClassOut]
    workspaces: list[str] = Field(description="Workspaces the consent covers; empty means all of yours.")
    include_new: bool
    standing_rules: list[dict[str, str]]
    directives: list[str]


class LearningIn(StrictModel):
    model_config = ConfigDict(extra="forbid")

    classes: list[str] = Field(description="Every class you agree to; at least one. Ending it all is DELETE.")
    workspaces: list[str] = Field(default_factory=list, description="Empty means every workspace you are in.")
    include_new: bool = True
    standing_rules: list[dict[str, str]] | None = Field(
        None, description="Deny-only rules, each naming the learner, purpose, class and/or workspace it denies. Omit to keep.")
    directives: list[str] | None = Field(None, description="Words every reader is shown. Omit to keep.")


class LearningSourceOut(StrictModel):
    session_id: uuid.UUID
    excluded: bool
