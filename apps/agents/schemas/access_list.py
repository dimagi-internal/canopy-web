from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictModel


class AccessLineIn(StrictModel):
    """One line to add: who, and what they get: ONE role, or (rarely) ONE named capability."""

    kind: Literal["person", "workspace", "domain", "contact"]
    role: str | None = None             # a role key from `GET .../agent-roles`; validated against the roles available here
    capability: str = ""                # instead of a role: open just this capability
    user_id: int | None = None
    email: str | None = None            # a person, by the address on their canopy account
    workspace: str = ""                 # kind=workspace: everyone in this workspace
    at_least: str = ""                  # ... who hold at least this workspace role ("" = any)
    include_below: bool = False         # ... or in any workspace below it (its own subtree)
    domain: str = ""                    # kind=domain/contact, or narrowing a workspace line
    verified: bool = True
    manual_only: bool = False
    note: str = Field(default="", max_length=300)


class AccessLinePatch(StrictModel):
    role: str | None = None
    manual_only: bool | None = None
    note: str | None = Field(default=None, max_length=300)


class AccessLineOut(StrictModel):
    id: int
    #: "agent" (this agent's own line) or "workspace" (a default it inherits from `scope_workspace`).
    scope: Literal["agent", "workspace"]
    scope_workspace: str | None = None
    kind: str
    who: str
    #: The named role the line grants ("" on a line that opens one capability instead).
    role: str
    #: The one capability the line opens ("" on a role line).
    capability: str = ""
    user_id: int | None = None
    subject_workspace: str | None = None
    at_least: str = ""
    include_below: bool = False
    domain: str = ""
    verified: bool = True
    manual_only: bool = False
    note: str = ""
    granted_by_email: str | None = None
    granted_at: dt.datetime | None = None
    can_edit: bool = False
    # Advice only (never a refusal) when the line can never match as written, e.g. a verified
    # address at a domain whose mail cannot prove its senders. Set when the line is added.
    warning: str = ""


class AccessImplicitOut(StrictModel):
    """A role nobody wrote on the list: it comes with the agent or the workspace."""

    who: str
    role: str
    why: str


class AccessRoleOut(StrictModel):
    #: The role's key (what a line stores and the envelope carries).
    role: str
    can: list[str]
    #: The permissions the role is made of (`read`, `write`, `agent_edit`, `use:<door>`; `admin`, `own` on the fixed roles).
    permissions: list[str] = Field(default_factory=list)
    #: The name a person sees; the key when the role is fixed in code.
    label: str = ""
    description: str = ""
    #: Does it let its holder drive or change the agent? The form disables it for lines that name people only
    #: by address, and for non-owners on a workspace default.
    contains_write: bool = False
    #: The workspace that defines it ("" for the roles fixed in code).
    defined_on: str = ""
    #: Fixed in code: `owner` and `admin` come with the agent, never from a line.
    fixed: bool = False


class AccessListOut(StrictModel):
    inherit_workspace_defaults: bool
    lines: list[AccessLineOut]
    implicit: list[AccessImplicitOut]
    roles: list[AccessRoleOut]
    #: Can the caller change this agent's list (an agent admin)?
    can_edit: bool = False
    #: The doors this agent has published (capabilities other than read mode), which a line may
    #: name; the form picks from these.
    doors: list[str] = Field(default_factory=list)


class AccessInheritIn(StrictModel):
    inherit: bool


class AgentDefaultsOut(StrictModel):
    """A workspace's default lines, the roles they can give, and what the caller may do."""

    lines: list[AccessLineOut]
    roles: list[AccessRoleOut]
    can_edit: bool = False          # a workspace admin
    can_give_write: bool = False    # the workspace owner: a default that lets people drive every agent
