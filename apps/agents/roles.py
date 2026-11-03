"""Agent permissions and the named roles made of them — the one table, and the only way to it.

Defined in `docs/superpowers/specs/2026-11-02-agent-permissions-and-roles-design.md`. A
**permission** is one thing a person may do with ONE agent; a **role** is a named bundle of them.
This module names every permission and every role, so the rest of the code asks
`allows(role, WRITE)` / `holds(permissions, WRITE)` and never spells a role set of its own.
The mechanism (closure, `allows`, rank) is `apps/common/rolesets.py`, shared with the workspace's
roles (`apps/workspaces/permissions.py`); the vocabulary is this agent's.

These are AGENT roles; a workspace has its own.
"""
from __future__ import annotations

from apps.common.rolesets import RoleSet

OWNER, ADMIN, EDITOR, READ_WRITE, READ_ONLY = "owner", "admin", "editor", "read_write", "read_only"
#: Not a person and not in the table: canopy itself, the agent's own login, an approved task's
#: dispatch. It may do what an admin may.
SYSTEM = "system"

# --- permissions ------------------------------------------------------------------

#: Ask the agent through its read-only capability (`ask`). Off until the owner publishes it.
READ = "read"
#: Drive the whole agent, in its own authority: every skill it has, every capability.
WRITE = "write"
#: Change the agent itself: its skills and capabilities.
AGENT_EDIT = "agent_edit"
#: Configure the agent in canopy-web: access list, capabilities, credentials, routing.
ADMINISTER = "admin"
#: Transfer or delete the agent, and name its admins. `Agent.owner`, never a line.
OWN = "own"
#: The scope of a named-capability permission: `use:<capability>` opens that one capability.
USE = "use"

#: The one capability `read` opens.
READ_CAPABILITY = "ask"

_PLAIN = (READ, WRITE, AGENT_EDIT, ADMINISTER, OWN)

#: THE table. Each role names what it holds; the dependency rule below closes it, so
#: `editor` really is read + write + agent_edit and `admin` everything but `own`.
_TABLE = {
    READ_ONLY: {READ},
    READ_WRITE: {WRITE},
    EDITOR: {AGENT_EDIT},
    ADMIN: {ADMINISTER},
    OWNER: {OWN, ADMINISTER},
}

#: The dependency rule: agent_edit implies write implies read; admin implies all but own; driving
#: the whole agent opens every capability.
_IMPLIES = {
    AGENT_EDIT: {WRITE},
    WRITE: {READ, f"{USE}:*"},
    ADMINISTER: {READ, WRITE, AGENT_EDIT},
}

ROLES = RoleSet("agent", _PLAIN, _TABLE, implies=_IMPLIES, scopes=(USE,),
                order=(READ_ONLY, READ_WRITE, EDITOR, ADMIN, OWNER))

#: Strongest first, for display.
LADDER = tuple(reversed(ROLES.order))
#: Roles an access-list line can grant. `admin` is `Agent.is_admin` and `owner` is `Agent.owner`:
#: neither is a line.
GRANTABLE = (EDITOR, READ_WRITE, READ_ONLY)
PERMISSIONS = frozenset(_PLAIN)


def permissions_of(role: str | None) -> frozenset[str]:
    return ROLES.permissions_of(role)


def allows(role: str | None, permission: str) -> bool:
    """May someone holding `role` do `permission`? None / an unknown role may do nothing."""
    return ROLES.allows(role, permission)


def holds(permissions, permission: str) -> bool:
    """Does a person holding the union `permissions` (several lines) hold `permission`?"""
    return ROLES.holds(permissions, permission)


def rank(role: str | None) -> int:
    """0 for no role, else higher is stronger."""
    return ROLES.rank(role)


def highest(roles) -> str | None:
    """The strongest of `roles`, or None when there are none."""
    return ROLES.strongest(roles)


def use(capability: str) -> str:
    """The scoped permission that opens one named capability."""
    return f"{USE}:{capability}"


def can_use(permissions, capability: str) -> bool:
    """May someone holding `permissions` use the capability `capability`? `write` opens all of
    them; `read` opens `ask`; `use:<name>` opens `<name>`."""
    if ROLES.holds(permissions, use(capability)):
        return True
    return capability == READ_CAPABILITY and ROLES.holds(permissions, READ)


def lowest_role_with(permission: str) -> str | None:
    """The weakest role that holds `permission` (e.g. the one a person needs to drive the agent)."""
    return ROLES.lowest_with(permission)
