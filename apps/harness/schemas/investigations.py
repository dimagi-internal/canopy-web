"""Failure investigation schemas."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from ninja import Schema
from pydantic import Field

InvestigationStatus = Literal["open", "held", "debugger_failed", "resolved", "pending_deploy", "escalated"]


class FailureInvestigationOut(Schema):
    """One kind of turn failure and what the debugger agent did about it."""

    id: int
    workspace_slug: str
    fingerprint: str
    status: str
    normalized_note: str
    sample_note: str
    occurrences: int
    first_seen: dt.datetime
    last_seen: dt.datetime
    turn_ids: list[str]
    agents: list[str]
    runners: list[str]
    recurred_after_resolve: bool
    debug_turn_id: uuid.UUID | None
    triggers: int
    resolved_at: dt.datetime | None
    resolution_note: str
    fix_commit: str = ""
    fix_committed_at: dt.datetime | None = None
    fix_pr: str = ""
    # The person it is with (status escalated), and what they are asked.
    handler_email: str = ""
    ask: str = ""
    # The failing runner as canopy saw it (machine-reported fields only).
    evidence: dict = Field(default_factory=dict)

    @staticmethod
    def resolve_workspace_slug(obj) -> str:
        return obj.workspace_id

    @staticmethod
    def resolve_handler_email(obj) -> str:
        return obj.handler.email if obj.handler_id and obj.handler else ""


class FailedTurnOut(Schema):
    """One failed turn of an investigation, as the people who debug it may see it:
    WHAT failed and in what kind of turn, never what anyone said. No prompt, no
    messages, no result text (the investigation's own note carries the error), no
    person's name or address. A private chat's turn is otherwise readable only by
    the people in that chat (`turn_queries.visible_turns_qs`), so without this the
    debugger could not tell a confined caller's chat from a full owner turn."""

    id: str
    agent: str = ""
    origin: str = ""
    #: The turn ran in a chat session (`private_chat`: someone's own conversation,
    #: whose content stays with its people) rather than as the agent's own turn.
    chat: bool = False
    private_chat: bool = False
    #: Who it was for, as a kind (user / contact / agent / system), how it came in,
    #: and how sure canopy was — never who.
    initiator_kind: str = ""
    initiator_via: str = ""
    initiator_assurance: str = ""
    #: The profile the claim ran it under (`Turn.capability`) and its turn mode.
    profile: str = ""
    turn_mode: str = ""
    runner: str = ""
    cli: str = ""
    status: str = ""
    created_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None


class FailureInvestigationDetailOut(FailureInvestigationOut):
    """One investigation, with each of its recent failed turns' metadata."""

    failed_turns: list[FailedTurnOut] = Field(default_factory=list)


class ResolveInvestigationIn(Schema):
    note: str = Field(min_length=1, max_length=10000,
                      description="What was wrong and what you changed to fix it.")
    fix_commit: str = Field(default="", pattern=r"^([0-9a-fA-F]{7,40})?$",
                            description="The commit that fixes it, when the fix is to the runner "
                                        "code that failed. A recurrence on a runner without it "
                                        "only counts.")
    fix_committed_at: dt.datetime | None = Field(
        default=None, description="That commit's committer time (`git log -1 --format=%cI`). "
                                  "Lets canopy tell a runner whose code is newer than the fix.")
    pr: str = Field(default="", max_length=300, description="The PR that carries the fix.")
    merged: bool | None = Field(default=None, description="Is that PR merged?")


class HandOffInvestigationIn(Schema):
    ask: str = Field(min_length=1, max_length=300, pattern=r"^[^\n]*$",
                     description="One line: what the person is needed for.")
    to: str = Field(default="", max_length=254,
                    description="The person's email. Default: the debugger agent's owner.")


# ---- Runner device keys (Canopy Vault) -------------
