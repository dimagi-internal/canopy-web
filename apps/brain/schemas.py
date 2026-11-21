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
    workspaces: list[str] | None = Field(
        None, description="Empty means every workspace you are in. Omit to keep. A workspace you have left is dropped.")
    include_new: bool | None = Field(
        None, description="With workspaces set: whether ones you join later are covered too. Omit to keep.")
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
    scope: str = Field("full", description="How much of a session it reads: full, conversation, userinput or opening.")


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
    scope: Literal["full", "conversation", "userinput", "opening"] = "full"


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
    scope: Literal["full", "conversation", "userinput", "opening"] | None = Field(
        None, description="May only narrow: full, conversation, userinput, opening.")


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


class BrainOutputSession(StrictModel):
    session_id: uuid.UUID
    workspace: str
    title: str = Field(description="Scrubbed of secrets.")


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
    sessions: list[BrainOutputSession] = Field(default_factory=list,
                                               description="The sessions it cites that you read yourself.")
    hidden_sessions: int = Field(0, description="The sessions it cites that you do not read: a count only.")


class BrainReadMessage(StrictModel):
    index: int
    role: str
    text: str
    author: dict | None = Field(None, description="Who wrote a person's line: user_id or contact_id, and name.")
    tool: dict | None = Field(None, description="On a tool call or its result: the tool's name.")


class BrainReadSource(StrictModel):
    source: int
    kind: str
    workspace: str
    title: str
    started_at: dt.datetime
    messages: list[BrainReadMessage]
    truncated: bool = Field(description="More of this source comes in the next call (`next_index`).")
    next_index: int | None = None
    directives: list[str] = Field(description="Words the people in this source asked every reader to be shown.")


class BrainReadOut(StrictModel):
    run: int
    learner: str
    purpose: str
    subject_type: str
    subject: str
    sources: list[BrainReadSource]
    view: str = Field("full", description="How much of each session is served: the feature's scope.")
    next_after: int = Field(description="Pass this as `after` to read on.")
    next_index: int | None = Field(None, description="When set, pass it as `index` with `next_after`: the rest "
                                   "of a source too large for one call.")
    done: bool = Field(description="Nothing is left past `next_after`.")
    project: dict | None = Field(None, description="For a project judge: its outcome and open tasks.")
    revoked: list[int] = Field(default_factory=list, description="Sources removed since this learner learned from "
                               "them: what came from them is withdrawn; do not rely on or rebuild it. Told once.")


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
    workspace: str = Field("", description="The session's workspace, for a link to its page.")


class ProjectSessionOut(StrictModel):
    session_id: uuid.UUID
    title: str
    workspace: str
    tier: str = Field(description="core | adjacent | candidate.")
    how: str = Field(description="dispatched | signals | judged | declared.")
    confidence: float | None = None
    started_at: dt.datetime


# ---- the relations graph ----------------------------------------------------------------------------------

class GraphNodeOut(StrictModel):
    """One thing in the graph. `ref` is what its own page is found by: a session's id, a project's handle, a task's
    key, an agent's slug, a person's user id, a workspace's slug, an output's id."""

    id: str
    type: Literal["person", "agent", "session", "project", "task", "workspace", "output"]
    label: str
    workspace: str = Field(description="Where it lives; blank for a person.")
    ref: str
    at: dt.datetime | None = Field(None, description="Its latest activity in the window, when it has one.")
    weight: int = Field(description="How many edges it has in this graph.")
    sensitive: bool = Field(False, description="A sensitive session: drawn with no title and nothing derived.")
    status: str = Field("", description="A task's status; an output's kind.")


class GraphEdgeOut(StrictModel):
    source: str
    target: str
    kind: Literal["participates", "started", "ran", "dispatched_from", "linked", "reports_to", "coaches",
                  "derived_from", "about", "within"]
    weight: float = Field(description="Turns, for `ran`, `participates` and `dispatched_from`; else 1.")
    at: dt.datetime | None = None
    tier: str = Field("", description="A `linked` edge's tier: core, adjacent, candidate, or rejected (your own call).")
    how: str = Field("", description="A `linked` edge's layer (dispatched, signals, judged, declared); a "
                                     "`reports_to` edge's workspace.")
    proposed: bool = Field(False, description="A link the brain WOULD make (`include=proposed`), stored nowhere.")
    evidence: list[str] = Field(default_factory=list)
    confidence: float | None = None


class GraphTotalsOut(StrictModel):
    nodes: dict[str, int]
    edges: dict[str, int]


class WorkspaceGraphOut(StrictModel):
    days: int
    since: dt.datetime
    workspaces: list[str]
    proposed: bool = Field(description="Whether the brain's would-be links are in it.")
    reporting: bool = Field(description="Whether reporting lines could be drawn: only to a person as themselves, "
                                        "never through MCP.")
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    totals: GraphTotalsOut
    notes: list[str] = Field(description="What a cap left out.")
    truncated: bool


# ---- the /brain page: the brain as it stands ------------------------------------------------------------------

class BrainConsentWorkspace(StrictModel):
    workspace: str
    name: str
    members: int
    agreed_any: int = Field(description="Members who agreed to at least one class for this workspace.")
    agreed: dict[str, int] = Field(description="Per class, members who agreed to it for this workspace.")


class BrainConsentState(StrictModel):
    mine: list[str] = Field(description="The classes you agreed to.")
    workspaces: list[BrainConsentWorkspace]


class BrainSourceCounts(StrictModel):
    workspace: str
    scope: Literal["workspace", "yours"] = Field(description="`workspace`: every source of it (you hold logs.read "
                                                 "there). `yours`: only sources you are in.")
    open: int
    withdrawn: int
    sealed: dict[str, int] = Field(description="Sealed sources by why, as the last refresh saw them.")


class BrainLinkGroup(StrictModel):
    project: str
    name: str
    workspace: str
    tier: str
    count: int


class BrainLearnerState(StrictModel):
    slug: str
    name: str
    purpose_class: str
    enabled: bool
    scope: str
    schedule: str
    operator: str
    accountable: str = Field(description="The canopy admin its runs act for, or empty: nobody active.")


class BrainRunCounts(StrictModel):
    open: int
    done: int
    failed: int


class BrainCascadeCounts(StrictModel):
    workspace: str
    scope: Literal["workspace", "yours"]
    rederive_open: int = Field(description="Outputs waiting to be derived again after losing a source.")
    revocations_undelivered: int = Field(description="Removals a learner has not been told of yet.")
    removed_recently: int = Field(description="Sessions taken out of learning in the last seven days.")
    outputs_withdrawn_recently: int
    outputs_stale_recently: int


class BrainStateOut(StrictModel):
    reads_enabled: bool = Field(description="BRAIN_READS_ENABLED: whether any learner may read a session.")
    workspace: str = Field("", description="The workspace the read is pinned to; it covers the ones below it too.")
    can_run: bool = Field(False, description="Whether you may start a learner's run here: its owner, or a canopy "
                          "admin.")
    workspaces: list[str]
    admin_workspaces: list[str] = Field(description="Where you see workspace-wide counts.")
    canopy_admin: bool
    consent: BrainConsentState
    sources: list[BrainSourceCounts]
    links: list[BrainLinkGroup] = Field(description="Stored links of sessions you read, by project and tier.")
    learners: list[BrainLearnerState]
    runs: BrainRunCounts
    cascade: list[BrainCascadeCounts]


class BrainStoredLink(StrictModel):
    id: int
    project: str
    name: str
    workspace: str
    tier: str
    how: str
    confidence: float | None = None
    evidence: list[str]
    session_id: uuid.UUID
    title: str
    started_at: dt.datetime


class BrainLinkPageOut(StrictModel):
    links: list[BrainStoredLink]
    next_before: int = Field(description="Pass this as `before` for the next page.")
    done: bool


class BrainRunSession(StrictModel):
    session_id: uuid.UUID
    workspace: str
    title: str


class BrainRunState(StrictModel):
    id: int
    learner: str
    purpose: str
    subject_type: str
    subject: str
    scope: str = Field("", description="The workspace it was started for: it reads that workspace and the ones below.")
    state: str
    created_at: dt.datetime
    finished_at: dt.datetime | None = None
    reads: int = Field(description="Sources it read.")
    sessions: list[BrainRunSession] = Field(description="The ones you read yourself.")
    hidden: int = Field(description="The ones you do not: a count only.")


class BrainRunPageOut(StrictModel):
    runs: list[BrainRunState]
    next_before: int
    done: bool


class RunNowIn(StrictModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field("", description="For a project learner: the workspace project's handle. Ignored otherwise.")
