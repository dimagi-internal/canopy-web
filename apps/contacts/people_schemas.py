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
    basis: str = Field(default="declared", description="declared | inferred.")
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

