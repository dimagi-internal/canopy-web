"""Wire shapes for a workspace's admission rules (/api/workspaces/{slug}/admission-rules)."""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictModel

Origin = Literal["", "api", "ace_web", "canopy_web_chat", "canopy_scheduler", "email", "slack"]
Principal = Literal["", "member", "contact", "agent", "system"]
Access = Literal["", "confined", "full", "none"]
Sender = Literal["", "blocked"]
TurnMode = Literal["", "manual", "auto"]
RunnerKind = Literal["", "emdash", "cloud", "remote"]
Reach = Literal["", "paused", "offline"]
Action = Literal["reject", "allow", "auto"]


class AdmissionRuleIn(StrictModel):
    """Blank conditions mean "any"; a rule needs at least one. `allow` exempts
    matching turns from every `reject` rule in the workspace. A rule on a
    blocked sender or `access: none` is about turns canopy always refuses: it
    sets what the refusal records and who is told, and cannot be an `allow`."""

    agent: str = Field("", description="An agent slug in this workspace, or blank for every agent.")
    origin: Origin = ""
    principal: Principal = ""
    sender: Sender = ""
    access: Access = ""
    capability: str = Field("", max_length=64)
    turn_mode: TurnMode = ""
    runner_kind: RunnerKind = ""
    reach: Reach = Field("", description="Turns no runner will take: every runner that could is "
                         "paused with no resume time, or none has been reachable for `wait_hours`.")
    wait_hours: int = Field(24, ge=1, le=24 * 30)
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
    sender: Sender | None = None
    access: Access | None = None
    capability: str | None = Field(None, max_length=64)
    turn_mode: TurnMode | None = None
    runner_kind: RunnerKind | None = None
    reach: Reach | None = None
    wait_hours: int | None = Field(None, ge=1, le=24 * 30)
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
    sender: str
    access: str
    capability: str
    turn_mode: str
    runner_kind: str
    reach: str
    wait_hours: int
    action: str
    reason: str
    notify_sender: bool
    notify_owner: bool
    enabled: bool
    summary: str
    #: Started as one of canopy's defaults (and is an ordinary row since).
    seeded: bool
    #: About turns canopy refuses on its own (a blocked sender, a person offered
    #: nothing). It cannot let them run; it sets what is recorded and who is told.
    refusal: bool
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
    senders: list[AdmissionChoice]
    access: list[AdmissionChoice]
    turn_modes: list[AdmissionChoice]
    runner_kinds: list[AdmissionChoice]
    reaches: list[AdmissionChoice]
    actions: list[AdmissionChoice]


class AdmissionRulesOut(StrictModel):
    workspace: str
    can_manage: bool
    #: Whether this workspace sends a rule's automatic reply to the sender. Off,
    #: it is recorded (`held`) and not sent. An admin switches it.
    sender_notices_enabled: bool
    rules: list[AdmissionRuleOut]
    choices: AdmissionChoices


class AdmissionSettingsIn(StrictModel):
    """Whether a rule's automatic reply to the sender is actually sent."""

    sender_notices_enabled: bool


class AdmissionSettingsOut(StrictModel):
    sender_notices_enabled: bool
