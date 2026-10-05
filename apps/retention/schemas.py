"""Wire shapes for a workspace's retention settings (/api/workspaces/{slug}/retention)."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictModel

# A workspace rule may be about chats or turns. Shared transcripts belong to no
# workspace, so a workspace cannot set their retention.
RuleKind = Literal["", "chat", "turn"]
RuleSource = Literal["", "api", "ace_web", "canopy_web_chat", "canopy_scheduler", "email", "slack",
                     "emdash"]
RulePrincipal = Literal["", "member", "contact", "agent", "system"]


class RetentionRuleIn(StrictModel):
    """Blank filters mean "any". `keep_days` null means keep forever: an
    exemption that beats a broader rule."""

    kind: RuleKind = ""
    source: RuleSource = ""
    principal: RulePrincipal = ""
    agent: str = Field("", description="An agent slug in this workspace, or blank for any agent.")
    keep_days: int | None = Field(None, ge=1, le=36500)
    note: str = Field("", max_length=2000)


class RetentionRuleOut(StrictModel):
    id: int
    #: The workspace the rule is set on; blank = deployment-wide.
    workspace: str
    kind: str
    source: str
    principal: str
    agent: str
    keep_days: int | None
    note: str
    summary: str
    created_by: str
    updated_at: dt.datetime


class RetentionChoice(StrictModel):
    value: str
    label: str


class RetentionChoices(StrictModel):
    kinds: list[RetentionChoice]
    sources: list[RetentionChoice]
    principals: list[RetentionChoice]


class RetentionOut(StrictModel):
    """This workspace's own rules, and those it inherits (ancestors nearest
    first, then deployment-wide). An item is governed by the nearest level that
    has ANY matching rule, then the most specific rule there, then the shorter
    retention. No match = kept forever."""

    workspace: str
    #: Whether this deployment purges at all. Off means the rules are saved and
    #: previewable, and nothing is deleted.
    enforced: bool
    can_manage: bool
    rules: list[RetentionRuleOut]
    inherited: list[RetentionRuleOut]
    choices: RetentionChoices


class RetentionPreviewRule(StrictModel):
    rule_id: int
    summary: str
    counts: dict[str, int]


class RetentionPreviewOut(StrictModel):
    """What the current rules would drop from this workspace's own chats and
    turns if they were enforced right now."""

    workspace: str
    enforced: bool
    totals: dict[str, int]
    by_rule: list[RetentionPreviewRule]
