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

import re
from typing import NamedTuple

from apps.agents.interface_default import READ_MODE_NAMES, is_read_mode
from apps.common.rolesets import RoleSet

OWNER, ADMIN, EDITOR, READ_WRITE, READ_ONLY = "owner", "admin", "editor", "read_write", "read_only"
#: Not a person and not in the table: canopy itself, the agent's own login, an approved task's
#: dispatch. It may do what an admin may.
SYSTEM = "system"

# --- permissions ------------------------------------------------------------------

#: Be answered by the agent in read mode: confined, on every agent from the start (`interface.is_read_mode`).
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

_PLAIN = (READ, WRITE, AGENT_EDIT, ADMINISTER, OWN)


class Seed(NamedTuple):
    """A built-in role a workspace catalog starts with (`AgentRole`, `builtin=True`): the three a
    line can give. Only the seeder and "reset to default" read it; a line reads its role's ROW."""

    key: str
    label: str
    description: str
    permissions: frozenset[str]


#: The built-in roles, weakest first. Rows are seeded from these on every root workspace
#: (`rolecatalog.ensure_seeded`, the signal on workspace creation, migration 0122) and are
#: editable and cloneable afterwards; the decision never reads this.
SEED = (
    Seed(READ_ONLY, "Read only", "Can ask the agent questions in read mode.", frozenset({READ})),
    Seed(READ_WRITE, "Read and write", "Can drive the whole agent.", frozenset({WRITE})),
    Seed(EDITOR, "Editor", "Can drive the whole agent and change its skills and capabilities.",
         frozenset({AGENT_EDIT})),
)
SEED_KEYS = tuple(s.key for s in SEED)

#: The roles fixed in code: not rows, not lines (`Agent.owner`, `Agent.is_admin`).
_FIXED = {
    ADMIN: {ADMINISTER},
    OWNER: {OWN, ADMINISTER},
}

#: What a custom role may be built from. `admin` and `own` come from who the agent belongs to,
#: never from an access line, so no role can hold them; `use:*` is what `write` is.
ROLE_PERMISSIONS = (READ, WRITE, AGENT_EDIT)
#: Keys no role may take: the fixed roles, and the words the access vocabulary retired.
RESERVED_KEYS = frozenset({OWNER, ADMIN, SYSTEM, "viewer", "user", "member", "contact"})

#: THE vocabulary table: the fixed roles and the defaults of the built-ins, closed by the
#: dependency rule below (`editor` is read + write + agent_edit, `admin` everything but `own`).
_TABLE = {**{s.key: set(s.permissions) for s in SEED}, **_FIXED}

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
_DOOR = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


def permissions_of(role: str | None) -> frozenset[str]:
    """What a FIXED role (`owner`, `admin`) holds, or a built-in's DEFAULT. Never what a line
    grants: a line reads its role's row (`AgentGrant.permissions`), which a person may have edited."""
    return ROLES.permissions_of(role)


class InvalidRole(ValueError):
    """A role definition the catalog refuses, in words fit to show beside the form."""


def validate_permissions(permissions) -> frozenset[str]:
    """The CLOSED permission set for a role being saved, or InvalidRole.

    A role is built from the fixed vocabulary at the read / write / agent_edit level: `read`,
    `write`, `agent_edit` and `use:<door>`. `admin` and `own` are `Agent.is_admin` and
    `Agent.owner`, never a line, so no role can hold them; `use:*` is what `write` is. The set is
    closed (write carries read and every door, agent_edit carries write) and stored closed."""
    wanted = {str(p) for p in permissions or ()}
    if not wanted:
        raise InvalidRole("a role must allow something")
    for p in sorted(wanted):
        if p in (ADMINISTER, OWN):
            raise InvalidRole(f"a role cannot allow {p!r}: that comes from who the agent belongs to "
                              "(its owner and its admins), never from an access line")
        scope, sep, door = p.partition(":")
        if p in ROLE_PERMISSIONS:
            continue
        if sep and scope == USE and door and door != "*" and _DOOR.match(door):
            continue
        raise InvalidRole(f"{p!r} is not something a role can allow: use read, write, agent_edit "
                          "or use:<door>")
    return ROLES.close(wanted)


def drives(permissions) -> bool:
    """Does a role with these permissions let its holder drive (or, which implies it, change) the
    agent? The one question every rail asks: it is by permission, never by a role's name."""
    return ROLES.holds(permissions, WRITE)


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
    them; `read` opens read mode; `use:<name>` opens `<name>`. Read mode is `read` and `ask`
    alike (`is_read_mode`): a line naming either opens both."""
    if is_read_mode(capability):
        return ROLES.holds(permissions, READ) or any(ROLES.holds(permissions, use(n))
                                                      for n in READ_MODE_NAMES)
    return ROLES.holds(permissions, use(capability))


def lowest_role_with(permission: str) -> str | None:
    """The weakest role that holds `permission` (e.g. the one a person needs to drive the agent)."""
    return ROLES.lowest_with(permission)
