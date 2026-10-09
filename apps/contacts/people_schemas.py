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

    workspace: str | None = Field(default=None, description="The workspace it was written in; "
                                                            "null for a personal entry.")
    asserted_by: str = Field(default="", description="The agent slug or the person's email that asserted it.")
    source_turn_id: str | None = None


class PersonRefOut(StrictModel):
    id: int
    display_name: str
    email: str = ""


class PersonOut(PersonRefOut):
    """A person, as one workspace knows them: live facts (corrections first)."""

    workspace: str
    facts: list[PersonFactOut] = Field(default_factory=list)
    see_all: str = "/people/me/"


class PersonAccessOut(StrictModel):
    """One read of what canopy knows about you."""

    created_at: str
    via: str = Field(description="envelope (handed to an agent with a turn) | api (looked up).")
    workspace: str | None = None
    reader_agent: str | None = None
    reader_user: str | None = None
    turn_id: str | None = None


class MemoryFeatureOut(StrictModel):
    available: bool = Field(description="May this be on at all. Off = no session can turn it on.")
    default: bool = Field(description="On in a session that says nothing (only when available).")
    changed_at: str | None = None


class AgentMemoryOut(StrictModel):
    """Your agent memory at the canopy level: two features, each with whether it is
    available and whether it is on by default in a new session. Turning one off
    deletes nothing."""

    record: MemoryFeatureOut = Field(description="Agents may learn about me (write).")
    use: MemoryFeatureOut = Field(description="Agents may use what they've learned (read).")


class SessionFeatureOut(StrictModel):
    available: bool
    default: bool
    override: bool | None = Field(description="This session's choice; null = use the default.")
    effective: bool = Field(description="What applies in this session: available AND "
                                        "(override, else default).")


class SessionMemoryOut(StrictModel):
    """Your agent memory as it applies in one session."""

    session_id: str
    record: SessionFeatureOut
    use: SessionFeatureOut


class PersonMeOut(PersonRefOut):
    """Everything canopy holds about the caller: live facts in every workspace,
    and the last 50 reads."""

    agent_memory: AgentMemoryOut
    facts: list[PersonFactDetailOut] = Field(default_factory=list)
    accesses: list[PersonAccessOut] = Field(default_factory=list)


class MemoryFeatureIn(StrictModel):
    available: bool | None = Field(default=None, description="Make it available (true) or not "
                                                             "(false). Omit to leave it.")
    default: bool | None = Field(default=None, description="On (true) or off (false) by default in "
                                                           "new sessions. Omit to leave it.")


class AgentMemoryIn(StrictModel):
    record: MemoryFeatureIn | None = Field(
        default=None, description="Agents may learn about me. Omit to leave it unchanged.")
    use: MemoryFeatureIn | None = Field(
        default=None, description="Agents may use what they've learned. Omit to leave it unchanged.")


class SessionMemoryIn(StrictModel):
    record: str | None = Field(default=None, description="on | off | inherit. Omit to leave it.")
    use: str | None = Field(default=None, description="on | off | inherit. Omit to leave it.")


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


class AgentCoverageOut(StrictModel):
    """How well the people brain served one agent over the window."""

    agent: str
    human_turns: int = Field(description="Turns with the agent a human started (not canopy, not another agent).")
    human_turns_with_context: int = Field(
        description="Of those, how many were handed a person block with at least one fact, "
                    "as recorded when the envelope was built.")
    context_rate: float | None = None
    facts_written: int = Field(description="Facts the agent recorded in-session: asserted by it, sourced from one of these human turns.")
    people: int = Field(description="Distinct people who started a turn with the agent.")
    healthy: bool
    reasons: list[str] = Field(default_factory=list, description="Why it is unhealthy, then anything worth saying.")


class PeopleCoverageOut(StrictModel):
    """Is the fleet brain alive in this workspace? A dead brain must be loud."""

    workspace: str
    days: int
    since: str
    generated_at: str
    rule: str = Field(description="The rule `healthy` applies, in words.")
    healthy: bool
    agents: list[AgentCoverageOut]
