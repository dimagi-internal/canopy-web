"""Wire shapes for a workspace's admission rules (/api/workspaces/{slug}/admission-rules)."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictModel

Origin = Literal["", "api", "ace_web", "canopy_web_chat", "canopy_scheduler", "email", "slack"]
Principal = Literal["", "member", "contact", "agent", "system"]
Access = Literal["", "confined", "full"]
TurnMode = Literal["", "manual", "auto"]
RunnerKind = Literal["", "emdash", "cloud", "remote"]
Action = Literal["reject", "allow"]


class AdmissionRuleIn(StrictModel):
    """Blank conditions mean "any"; a rule needs at least one. `allow` exempts
    matching turns from every `reject` rule in the workspace."""

    agent: str = Field("", description="An agent slug in this workspace, or blank for every agent.")
    origin: Origin = ""
    principal: Principal = ""
    access: Access = ""
    capability: str = Field("", max_length=64)
    turn_mode: TurnMode = ""
    runner_kind: RunnerKind = ""
    action: Action = "reject"
    reason: str = Field(min_length=1, max_length=2000)
    notify_sender: bool = False
    notify_owner: bool = True
    enabled: bool = True


class AdmissionRulePatch(StrictModel):
    """Only the fields sent change — so the enabled switch is `{"enabled": false}`."""

    agent: str | None = None
    origin: Origin | None = None
    principal: Principal | None = None
    access: Access | None = None
    capability: str | None = Field(None, max_length=64)
    turn_mode: TurnMode | None = None
    runner_kind: RunnerKind | None = None
    action: Action | None = None
    reason: str | None = Field(None, min_length=1, max_length=2000)
    notify_sender: bool | None = None
    notify_owner: bool | None = None
    enabled: bool | None = None


class AdmissionRuleOut(StrictModel):
    id: int
    workspace: str
    agent: str
    origin: str
    principal: str
    access: str
    capability: str
    turn_mode: str
    runner_kind: str
    action: str
    reason: str
    notify_sender: bool
    notify_owner: bool
    enabled: bool
    summary: str
    #: Started as one of canopy's defaults (and is an ordinary row since).
    seeded: bool
    fired_count: int
    last_fired_at: dt.datetime | None
    created_by: str
    updated_at: dt.datetime


class AdmissionChoice(StrictModel):
    value: str
    label: str


class AdmissionChoices(StrictModel):
    origins: list[AdmissionChoice]
    principals: list[AdmissionChoice]
    access: list[AdmissionChoice]
    turn_modes: list[AdmissionChoice]
    runner_kinds: list[AdmissionChoice]
    actions: list[AdmissionChoice]


class AdmissionRulesOut(StrictModel):
    workspace: str
    can_manage: bool
    #: Whether the sender's automatic reply is switched on for this deployment.
    #: Off, a rule's sender notice is recorded and not sent.
    sender_notices_enabled: bool
    rules: list[AdmissionRuleOut]
    choices: AdmissionChoices
