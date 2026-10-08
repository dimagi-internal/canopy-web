"""Pydantic schemas for /api/people — what agents know about a person (canopy#804)."""
from __future__ import annotations

from pydantic import Field

from apps.common.schemas import StrictModel


class PersonProjectRef(StrictModel):
    id: int
    title: str = Field(description="The agent project's name.")
    ext_id: str = Field(default="", description="Its per-agent id (P1, P2 …).")


class PersonFactOut(StrictModel):
    """One live, work-context fact."""

    id: int
    kind: str = Field(description="role | project | instance | preference | correction | terminology.")
    statement: str
    basis: str = Field(description="declared (the person said it, or a human asserted it) | inferred (a model concluded it).")
    project: PersonProjectRef | None = None
    instance_ref: str = ""
    created_at: str | None = None
    # HCP v1 (apps/contacts/hcp.py): the entry this fact is a version of.
    entry_id: str | None = Field(default=None, description="urn:uuid:<id>, the same on every version.")
    version: int = 1
    category: str = Field(default="work_context", description="The HCP category — the grant scope.")
    dimension: str | None = None
    confidence: str | None = Field(default=None, description="high | medium | low — model-inferred only.")
    status: str = Field(default="active", description="active | deprecated | conflicted | deleted.")


class PersonFactDetailOut(PersonFactOut):
    """A fact as its subject sees it on their own page: where and who."""

    workspace: str
    asserted_by: str = Field(default="", description="The agent slug or the person's email that asserted it.")
    source_turn_id: str | None = None


class PersonRefOut(StrictModel):
    id: int
    display_name: str
    email: str = ""


class PersonOut(PersonRefOut):
    """A person, as one workspace knows them: live facts (corrections first) and the digest."""

    workspace: str
    digest: str = ""
    digest_updated_at: str | None = None
    facts: list[PersonFactOut] = Field(default_factory=list)
    see_all: str = "/people/me/"


class PersonDigestOut(StrictModel):
    workspace: str
    text: str
    updated_at: str | None = None
    updated_by: str = ""


class PersonAccessOut(StrictModel):
    """One read of what canopy knows about you."""

    created_at: str
    via: str = Field(description="envelope (handed to an agent with a turn) | api (looked up).")
    workspace: str | None = None
    reader_agent: str | None = None
    reader_user: str | None = None
    turn_id: str | None = None


class PersonMeOut(PersonRefOut):
    """Everything canopy holds about the caller: live facts in every workspace,
    every digest, and the last 50 reads."""

    facts: list[PersonFactDetailOut] = Field(default_factory=list)
    digests: list[PersonDigestOut] = Field(default_factory=list)
    accesses: list[PersonAccessOut] = Field(default_factory=list)


class PersonFactIn(StrictModel):
    workspace: str | None = Field(
        default=None, description="The workspace slug the fact is written in (default: the /api/w/{ws}/ one).")
    kind: str = Field(description="role | project | instance | preference | correction | terminology — anything else is a 400.")
    statement: str = Field(description="One sentence, 1–500 characters.")
    basis: str = Field(default="declared", description="declared (the person said it) | inferred (a model concluded it) | attested (someone else asserted it).")
    category: str | None = Field(default=None, description="HCP category (default: from kind) — work_context, general_preferences, goals_and_constraints, coordination_context or hcp-custom:<name>.")
    dimension: str | None = Field(default=None, description="HCP dimension within the category; an inferred and a declared fact on the same dimension conflict, and the inference is quarantined.")
    confidence: str | None = Field(default=None, description="high | medium | low — for an inferred fact.")
    source_turn_id: str | None = None
    project_id: int | None = None
    instance_ref: str = Field(default="", max_length=300)
    supersedes_id: int | None = Field(
        default=None, description="A live fact about the same person in the same workspace that this one replaces.")


class PersonFactCreatedOut(PersonFactOut):
    supersedes_id: int | None = None


class PersonDigestIn(StrictModel):
    workspace: str | None = None
    text: str = Field(max_length=2000)
    source_turn_ids: list[str] = Field(default_factory=list)


class PersonConversationOut(StrictModel):
    """A turn this person started with the agent."""

    id: str
    created_at: str | None = None
    origin: str
    via: str = ""
    status: str
    prompt: str = Field(default="", description="Their message, cut at 4000 characters.")
    result_note: str = ""
    chat_session_id: str | None = None
    content_purged: bool = Field(default=False, description="Retention scrubbed this turn's content.")


class PersonConversationsOut(StrictModel):
    person: int
    agent: str
    conversations: list[PersonConversationOut]



class PersonProjectOut(StrictModel):
    """A project (of one of the workspace's agents) the person takes part in."""

    id: int
    ext_id: str = Field(description="Its per-agent id (P1, P2 …).")
    name: str
    agent: str = Field(description="The slug of the agent whose project it is.")
    status: str = Field(description="active | done | archived.")
    role: str = Field(default="", description="The part they play, free text; blank when canopy linked them itself.")
    source: str = Field(description="fact | turn | manual — how canopy first learned it.")
    since: str | None = None


class PersonProjectsOut(StrictModel):
    person: int
    workspace: str
    projects: list[PersonProjectOut]


class DigestTurnCountsOut(StrictModel):
    queued: int = Field(description="Not finished yet (queued, claimed, running, needs human).")
    done: int
    failed: int = Field(description="Failed, lost or missed.")
    cancelled: int


class AgentCoverageOut(StrictModel):
    """How well the people brain served one agent over the window."""

    agent: str
    digest_enabled: bool = Field(description="This agent's switch AND the fleet-wide one.")
    human_turns: int = Field(description="Turns with the agent a human started (not canopy, not another agent).")
    human_turns_with_context: int = Field(
        description="Of those, how many were handed a person block with at least one fact or a digest, "
                    "as recorded when the envelope was built.")
    context_rate: float | None = None
    digest_turns: DigestTurnCountsOut
    digest_failure_rate: float | None = Field(default=None, description="failed / (done + failed); null with none finished.")
    facts_written: int = Field(description="Facts the agent asserted in this workspace.")
    people: int = Field(description="Distinct people who started a turn with the agent.")
    people_with_digest: int
    median_digest_age_hours: float | None = Field(
        default=None, description="Median age of those people's digests now; null when none has one.")
    healthy: bool
    reasons: list[str] = Field(default_factory=list, description="Why it is unhealthy, then anything worth saying.")


class PeopleCoverageOut(StrictModel):
    """Is the fleet brain alive in this workspace? A dead brain must be loud."""

    workspace: str
    days: int
    since: str
    generated_at: str
    digest_enabled_globally: bool
    rule: str = Field(description="The rule `healthy` applies, in words.")
    healthy: bool
    agents: list[AgentCoverageOut]
