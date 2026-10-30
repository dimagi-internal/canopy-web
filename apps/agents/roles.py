"""The agent role ladder and what each role may do — the one table, and the only way to it.

Defined once in `docs/superpowers/specs/2026-10-30-agent-roles-and-access-list-design.md`.
A **role** is what a person may do with ONE agent; a **permission** is what a role is made
of. This module names every role and every permission, so the rest of the code asks
`allows(role, WRITE)` or `at_least(role, EDITOR)` and never spells a role set of its own.
These are AGENT roles; a workspace has its own (`apps/workspaces/permissions.py`).
"""
from __future__ import annotations

OWNER, ADMIN, EDITOR, USER, VIEWER = "owner", "admin", "editor", "user", "viewer"
#: Not a person and not on the ladder: canopy itself, the agent's own login, an approved
#: task's dispatch. It may do what an admin may.
SYSTEM = "system"

#: Strongest first. `system` (canopy itself, the agent's own login) is not a person and
#: has no place on this ladder.
LADDER = (OWNER, ADMIN, EDITOR, USER, VIEWER)
RANK = {role: len(LADDER) - i for i, role in enumerate(LADDER)}

#: Roles an access-list line can grant. `owner` is `Agent.owner`, never a line.
GRANTABLE = (ADMIN, EDITOR, USER, VIEWER)

# --- permissions ------------------------------------------------------------------

#: Open a capability published for viewers: `ask`.
READ = "read"
#: Open a capability published for users, including one that acts.
WRITE = "write"
#: Steer the whole agent, in its own authority — every skill it has.
RUN = "run"
#: Change the agent itself: its skills and capabilities.
EDIT = "edit"
#: Configure the agent in canopy-web: access list, capabilities, credentials, routing.
ADMINISTER = "admin"
#: Transfer or delete the agent, and name its admins.
OWN = "own"

_BY_ROLE: dict[str, frozenset[str]] = {
    VIEWER: frozenset({READ}),
    USER: frozenset({READ, WRITE}),
    EDITOR: frozenset({READ, WRITE, RUN, EDIT}),
    ADMIN: frozenset({READ, WRITE, RUN, EDIT, ADMINISTER}),
    OWNER: frozenset({READ, WRITE, RUN, EDIT, ADMINISTER, OWN}),
}
PERMISSIONS = frozenset().union(*_BY_ROLE.values())


def rank(role: str | None) -> int:
    """0 for no role, else higher is stronger."""
    return RANK.get(role or "", 0)


def at_least(role: str | None, floor: str) -> bool:
    return rank(role) >= RANK[floor]


def allows(role: str | None, permission: str) -> bool:
    """May someone holding `role` do `permission`? None / an unknown role may do nothing."""
    if permission not in PERMISSIONS:
        raise ValueError(f"unknown agent permission {permission!r}")
    return permission in _BY_ROLE.get(role or "", frozenset())


def highest(roles) -> str | None:
    """The strongest of `roles`, or None when there are none."""
    best = max((r for r in roles if r in RANK), key=rank, default=None)
    return best
