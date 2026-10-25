"""Pydantic schemas for /api/people — what agents know about a person (canopy#804)."""
from __future__ import annotations

import datetime as dt
from typing import Any

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
    # HCP v1 (apps/contacts/hcp/): the entry this fact is a version of.
    entry_id: str | None = Field(default=None, description="urn:uuid:<id>, the same on every version.")
    version: int = 1
    category: str = Field(default="work_context", description="The HCP category — the grant scope.")
    dimension: str | None = None
    confidence: str | None = Field(default=None, description="high | medium | low — model-inferred only.")
    status: str = Field(default="active", description="active | deprecated | conflicted | deleted.")


class FactUseOut(StrictModel):
    """How often agents were actually told an entry — over its whole history, not a page of it."""

    conversations: int = Field(default=0, description="Distinct turns an agent was told it in.")
    last_at: str | None = Field(default=None, description="The last time an agent was told it.")
    by: list[str] = Field(default_factory=list, description="Agent slugs that were told it.")


class PersonFactDetailOut(PersonFactOut):
    """A fact as its subject sees it on their own page: where and who."""

    used: FactUseOut = Field(default_factory=FactUseOut)

    workspace: str | None = Field(default=None, description="The workspace it was written in; "
                                                            "null for a personal entry.")
    workspace_name: str = Field(default="", description="That workspace's display name.")
    org: str | None = Field(default=None, description="The organisation (top of the workspace tree) "
                                                      "it lives in; null for a personal entry.")
    org_name: str = Field(default="", description="The organisation's display name.")
    asserted_by: str = Field(default="", description="The agent slug or the person's email that asserted it.")
    source_turn_id: str | None = None
    learned_by: str = Field(default="", description="The agent's name, when an agent recorded it.")
    provenance_site: dict = Field(default_factory=dict,
                                  description="The connected site the agent was reached through "
                                              "when it recorded this — {name, page?} — or {}.")


class PersonRefOut(StrictModel):
    id: int
    display_name: str
    email: str = ""


class PersonOut(PersonRefOut):
    """A person, as one workspace knows them: live facts (corrections first)."""

    workspace: str
    facts: list[PersonFactOut] = Field(default_factory=list)
    see_all: str = "/people/me/"


class PersonAddressOut(StrictModel):
    """One more way to reach a person — theirs, across every workspace."""

    id: int
    kind: str = Field(description="email | phone.")
    label: str = Field(description="What it is for: work, personal, school …")
    value: str
    created_at: str | None = None


class PersonAddressesOut(StrictModel):
    person: int
    workspace: str
    addresses: list[PersonAddressOut] = Field(default_factory=list)


class MemberAddressesOut(StrictModel):
    """One person in the workspace, and every other way to reach them."""

    person_id: int
    user_id: int
    email: str = Field(description="The address that identifies them (their login).")
    display_name: str = ""
    may_edit: bool = Field(description="Is this the caller? Only a person changes their own addresses.")
    addresses: list[PersonAddressOut] = Field(default_factory=list)


class WorkspaceAddressesOut(StrictModel):
    workspace: str
    people: list[MemberAddressesOut] = Field(default_factory=list)


class PersonAddressIn(StrictModel):
    kind: str = Field(description="email | phone — anything else is a 400.")
    label: str = Field(description="What it is for, 1–40 characters: work, personal, school …")
    value: str = Field(description="The address or number. The same kind and value again relabels it.")


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

    record: MemoryFeatureOut = Field(description="Learn from me: agents may add to your memory (write).")
    use: MemoryFeatureOut = Field(description="Use my memory: agents may read your memory (read).")


class SessionGrantOut(StrictModel):
    """A grant you gave this session's agent (HCP 4.1.5)."""

    grant_id: str
    type: str = Field(description="temporary (this session) | persistent (always, until revoked).")
    expires_at: str | None = None
    reach: str = Field(default="", description="The part of your memory it reaches, in words.")


class SessionFeatureOut(StrictModel):
    available: bool
    default: bool
    override: bool | None = Field(description="This session's choice; null = use the default.")
    effective: bool = Field(description="What applies in this session: available AND "
                                        "(override, else default).")
    granted: bool = Field(default=False, description="This session's agent holds a live grant for it.")
    grant: SessionGrantOut | None = None


class SessionAgentOut(StrictModel):
    slug: str
    name: str


class SessionMemoryOut(StrictModel):
    """Your agent memory as it applies in one session, and what you have granted
    the session's agent. A feature that is on here but not granted is what the
    session asks you about."""

    session_id: str
    record: SessionFeatureOut
    use: SessionFeatureOut
    agent: SessionAgentOut | None = Field(default=None, description="The agent a grant here goes to.")
    categories: list[str] = Field(default_factory=list,
                                  description="The categories a grant covers (shown before you grant).")
    reach: str = Field(default="", description="What a grant here would reach, in words — by "
                                               "default the agent's organisation, all divisions "
                                               "(HCP 4.1.5.1). Derived, never chosen here.")
    org: str = Field(default="", description="The organisation the agent serves, e.g. 'Dimagi'.")
    session_grant_hours: int = Field(default=24, description="A 'for this session' grant lasts at "
                                                             "most this long, or until the session ends.")
    memory_enabled: bool = Field(default=True, description="Is memory on for you at all yet? False = outside the rollout: nothing is offered, every switch reads off, and nothing can be turned on or granted (settings.HCP_PEOPLE).")


class AgentGrantIn(StrictModel):
    features: list[str] = Field(description="record and/or use.")
    duration: str = Field(description="session (temporary, this session only) | always (this "
                                      "agent, until you revoke it).")
    surface: str = Field(default="chat", description="chat | widget — where you gave it.")


class MemoryContextOut(StrictModel):
    source: str = Field(description="tree:<organisation> (all of it, as it is when read) | "
                                    "workspace:<slug> | personal (entries in no workspace).")
    label: str = Field(description='A short name: "All of Dimagi", "Connect", "No workspace".')
    group: str = Field(default="", description="The organisation it sits under, by name; "
                                               "empty for no workspace.")


class MemoryCollectionUserOut(StrictModel):
    grant_id: str
    agent: str
    name: str
    type: str


class MemoryCollectionOut(StrictModel):
    """A named set of contexts you can point an agent at."""

    id: int
    name: str
    sources: list[str]
    reach: str = Field(description="What it holds, in words.")
    used_by: list[MemoryCollectionUserOut] = Field(default_factory=list)
    updated_at: str | None = None


class MemoryCollectionsOut(StrictModel):
    collections: list[MemoryCollectionOut] = Field(default_factory=list)
    contexts: list[MemoryContextOut] = Field(default_factory=list,
                                             description="What a collection of yours may hold.")


class MemoryCollectionIn(StrictModel):
    name: str
    sources: list[str] = Field(description="Any of the contexts: tree:<organisation>, "
                                           "workspace:<slug>, personal.")


class SearchPreviewIn(StrictModel):
    query: str
    floor: float | None = Field(default=None, ge=0, le=1,
                                description="Try this cutoff instead of HCP_SEMANTIC_FLOOR.")
    limit: int = Field(default=8, ge=1, le=20)


class SearchPreviewEntryOut(StrictModel):
    entry_id: str | None
    statement: str | None = Field(description="What the entry says now; null once it was deleted.")
    kind: str
    project: str | None
    meaning: float | None = Field(description="Cosine similarity to the question, 0-1; null when not compared.")
    words: list[str] = Field(description="Words shared with the question.")
    on_project: bool
    total: float
    position: int | None = Field(description="1-based place in what an agent would get; null if left out.")
    because: str


class SearchPreviewOut(StrictModel):
    method: str = Field(description="hybrid (words and meaning) or lexical (words only).")
    model: str
    floor: float = Field(description="The cutoff this preview used.")
    live_floor: float = Field(description="HCP_SEMANTIC_FLOOR — what agents' searches use.")
    weight: float
    project_boost: float
    limit: int
    embed_ms: int | None
    entries: list[SearchPreviewEntryOut]


class SearchRecordOut(StrictModel):
    retrieval_id: str
    at: dt.datetime
    actor_id: str
    actor_type: str
    agent: str | None = Field(description="The agent whose turn searched, by name.")
    purpose: str
    query: str = Field(description="The question searched with (a turn's own context search: its prompt).")
    method: str
    model: str
    floor: float | None
    weight: float | None
    project_boost: float | None
    embed_ms: int | None
    scored: bool = Field(description="False: from before scores were kept — what came back only.")
    turn_id: str | None
    session_href: str | None
    session_title: str | None
    entries: list[SearchPreviewEntryOut]


class SearchHistoryOut(StrictModel):
    searches: list[SearchRecordOut]
    total: int | None
    next_before: str | None


class MemoryCollectionUpdateIn(StrictModel):
    name: str | None = None
    sources: list[str] | None = None
    confirm_widening: list[str] | None = Field(
        default=None, description="Adding a context lets the agents using this reach more: list "
                                  "the grants the refusal named to confirm.")


class GrantCollectionIn(StrictModel):
    collection_id: int | None = Field(description="One of your collections; null = the agent's "
                                                  "default (its own organisation).")
    confirm_widening: list[str] | None = None


class PersonMeOut(PersonRefOut):
    """Everything canopy holds about the caller: live facts in every workspace,
    and the last 50 reads."""

    agent_memory: AgentMemoryOut
    memory_enabled: bool = Field(default=True, description="Is memory on for you at all yet? False = outside the rollout: nothing is offered, every switch reads off, and nothing can be turned on or granted (settings.HCP_PEOPLE).")
    memory_paused: bool = Field(default=False, description="Memory is not on for you, but canopy "
                                "still holds some of yours: you can see, download and delete it.")
    facts: list[PersonFactDetailOut] = Field(default_factory=list)
    addresses: list[PersonAddressOut] = Field(
        default_factory=list, description="Every other way you can be reached — yours to change.")
    accesses: list[PersonAccessOut] = Field(default_factory=list)


class MemoryFeatureIn(StrictModel):
    available: bool | None = Field(default=None, description="Make it available (true) or not "
                                                             "(false). Omit to leave it.")
    default: bool | None = Field(default=None, description="On (true) or off (false) by default in "
                                                           "new sessions. Omit to leave it.")


class DefaultAgentIn(StrictModel):
    agent: str = Field(description="The agent's slug, or \"\" to clear your default.")


class DefaultAgentOut(StrictModel):
    agent: str = Field(description="Your default agent's slug, or \"\" for none.")
    name: str = Field(default="", description="Its name.")


class AgentMemoryIn(StrictModel):
    record: MemoryFeatureIn | None = Field(
        default=None, description="Learn from me. Omit to leave it unchanged.")
    use: MemoryFeatureIn | None = Field(
        default=None, description="Use my memory. Omit to leave it unchanged.")


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


# --- letting an agent see your memory page (apps/contacts/viewers.py) ---------------


class MemoryViewerIn(StrictModel):
    agent: str = Field(description="The slug of an agent in a workspace you belong to.")


class MemoryViewerOut(StrictModel):
    agent: str
    name: str
    granted_at: dt.datetime
    last_viewed_at: dt.datetime | None = Field(
        None, description="When it last looked (one look per 10 minutes is recorded).")


class ViewerChoiceOut(StrictModel):
    slug: str
    name: str


class MemoryViewersOut(StrictModel):
    viewers: list[MemoryViewerOut]
    agents: list[ViewerChoiceOut] = Field(description="Agents you could let in.")


class SharedPageOut(StrictModel):
    person_id: int
    display_name: str
    granted_at: dt.datetime
    href: str = Field(description="The page, in canopy's web app.")


class SharedPagesOut(StrictModel):
    pages: list[SharedPageOut]


class MemoryPageOut(StrictModel):
    me: PersonMeOut
    grants: list[dict[str, Any]] = Field(description="The person's HCP grants, as /api/hcp/v1/grants shows them.")
    collections: MemoryCollectionsOut


class MemoryPageAuditOut(StrictModel):
    events: list[dict[str, Any]] = Field(description="Audit events, as /api/hcp/v1/audit shows them.")
    nextCursor: str | None = None
