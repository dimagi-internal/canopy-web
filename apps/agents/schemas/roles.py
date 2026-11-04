from __future__ import annotations

from pydantic import Field

from apps.common.schemas import StrictModel


class AgentRoleIn(StrictModel):
    """A role to define: a name and what it allows, chosen from read, write, agent_edit and use:<door>."""

    key: str = Field(max_length=40)
    label: str = Field(max_length=80)
    description: str = Field(default="", max_length=300)
    permissions: list[str]


class AgentRolePatch(StrictModel):
    label: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    permissions: list[str] | None = None


class AgentRoleUseOut(StrictModel):
    lines: int
    agents: int
    defaults: int


class AgentRoleOut(StrictModel):
    key: str
    label: str
    description: str = ""
    #: The closed set the role allows (`read`, `write`, `agent_edit`, `use:<door>`).
    permissions: list[str]
    #: What it lets someone do, in words.
    can: list[str]
    defined_on: str
    #: Seeded with every organisation: renamable and reshapable, never deletable, resettable.
    builtin: bool = False
    #: Does it let its holder drive or change the agent (write, which agent_edit carries)? Only a
    #: workspace owner defines such a role, and no address-only line can hold it.
    contains_write: bool = False
    in_use: AgentRoleUseOut
    #: May the caller edit and delete this one (it is defined on a workspace they administer)?
    can_edit: bool = False


class FixedRoleOut(StrictModel):
    key: str
    label: str
    can: list[str]
    why: str


class AgentRolesOut(StrictModel):
    roles: list[AgentRoleOut]
    #: `owner` and `admin`: they come from who the agent belongs to, not from a line, so no role holds them.
    fixed: list[FixedRoleOut]
    #: May the caller define roles on this workspace (a workspace admin)?
    can_edit: bool = False
    #: May the caller define one that drives or changes the agent (the workspace owner)?
    can_give_write: bool = False
