"""Pydantic schemas for /api/me/learning."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

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


class SourceLinkOut(StrictModel):
    project: str = Field(description="The project's handle in the session's workspace.")
    name: str
    tier: str = Field(description="core | adjacent | candidate | rejected.")
    how: str = Field(description="dispatched | signals | judged | declared.")


class LearningSourceOut(StrictModel):
    session_id: uuid.UUID
    excluded: bool
    projects: list[SourceLinkOut] = Field(
        default_factory=list, description="Projects of yours this session is tied to; only ones you may read.")


class PersonLinkIn(StrictModel):
    model_config = ConfigDict(extra="forbid")

    tier: Literal["core", "rejected"] = Field(description="`core`: it belongs to the project. `rejected`: it does not, and stays rejected.")


class AgentLinkIn(StrictModel):
    model_config = ConfigDict(extra="forbid")

    tier: Literal["core", "adjacent"]


class CapabilityOut(StrictModel):
    slug: str
    name: str
    description: str
    purpose_class: str
    subject_type: str
    purposes: list[str]
    source_kinds: list[str]
    output_kinds: list[str]
    audience: list[str]
    operator: str = Field("", description="The agent that runs it, by slug.")
    entry: str = Field("", description="The plugin command a run starts with.")
    schedule: str
    enabled: bool


class CapabilityIn(StrictModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    name: str
    description: str = ""
    purpose_class: str
    purposes: list[str]
    source_kinds: list[str] = Field(default_factory=lambda: ["session"])
    output_kinds: list[str] = Field(default_factory=list)
    audience: list[str] | None = Field(None, description="At most its class's audience; omitted means all of it.")
    operator: str = Field("", description="An agent's slug, or empty.")
    entry: str = Field("", description="One plugin command, `/<plugin>:<skill> [args]`.")
    schedule: str = ""
    enabled: bool = False


class CapabilityPatch(StrictModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    purposes: list[str] | None = None
    source_kinds: list[str] | None = None
    output_kinds: list[str] | None = None
    audience: list[str] | None = Field(None, description="May only narrow what the class allows.")
    operator: str | None = None
    entry: str | None = None
    schedule: str | None = None
    enabled: bool | None = None


class ReportingLineOut(StrictModel):
    person_id: int
    person: str
    manager_id: int
    manager: str
    since: dt.datetime


class ReportingLineIn(StrictModel):
    model_config = ConfigDict(extra="forbid")

    person: str = Field(description="The person's email; a member of the workspace.")
    manager: str = Field(description="Their manager's email; a member of the workspace.")


class BrainOutputOut(StrictModel):
    id: int
    learner: str = Field(description="The slug of the learning feature that produced it.")
    kind: str
    workspace: str
    subject_type: str
    subject: str
    title: str
    body: dict
    state: str = Field(description="active | stale (a source it came from was removed).")
    created_at: dt.datetime


class BrainReadMessage(StrictModel):
    index: int
    role: str
    text: str
    author_user_id: int | None = None


class BrainReadSource(StrictModel):
    source: int
    kind: str
    workspace: str
    title: str
    started_at: dt.datetime
    messages: list[BrainReadMessage]
    truncated: bool
    directives: list[str] = Field(description="Words the people in this source asked every reader to be shown.")


class BrainReadOut(StrictModel):
    run: int
    learner: str
    purpose: str
    subject_type: str
    subject: str
    sources: list[BrainReadSource]
    next_after: int = Field(description="Pass this as `after` to read on.")
    done: bool = Field(description="Nothing is left past `next_after`.")
    project: dict | None = Field(None, description="For a project judge: its outcome and open tasks.")


class BrainEmitIn(StrictModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="One of the output kinds this run's feature declares.")
    title: str = ""
    body: dict
    sources: list[int] = Field(description="The `source` ids this output came from; each must have been read in this run.")


class BrainEmitOut(StrictModel):
    id: int
    state: str


class DisclosureDayOut(StrictModel):
    day: dt.date
    learner: str = Field(description="Which learner read.")
    reads: int
    sources: int = Field(description="How many of your sessions those reads covered.")


class RemovedSessionOut(StrictModel):
    session_id: uuid.UUID
    title: str = ""


class ProjectSessionOut(StrictModel):
    session_id: uuid.UUID
    title: str
    workspace: str
    tier: str = Field(description="core | adjacent | candidate.")
    how: str = Field(description="dispatched | signals | judged | declared.")
    confidence: float | None = None
    started_at: dt.datetime
