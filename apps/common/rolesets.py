"""One role -> permission-set engine, shared by agent roles and workspace roles.

Share the MECHANISM, not the vocabulary: `apps/agents/roles.py` (read, write, agent_edit, ...) and
`apps/workspaces/permissions.py` (logs.read, members.manage, ...) each declare their own
permissions and roles and build a `RoleSet`; this module knows no role or permission by name.

A `RoleSet` is a vocabulary of permissions, a table of named roles (each a set of permissions) and an
implication rule (`write` implies `read`). Roles are CLOSED under the rule when the set is built, so a
role can never hold a permission without the ones it depends on, and a future user-defined role gets
the same guarantee by going through the same constructor.

Two ways to declare the table:

* `RoleSet(name, permissions, roles, implies)`: each role names the permissions it holds.
* `RoleSet.ladder(name, permissions, steps)`: roles weakest first, each ADDING permissions to the
  one below (the workspace's viewer < editor < admin < owner).

A scoped permission (`use:connect`) is `<scope>:<name>`; the scope must be declared in `scopes`.
Holding `<scope>:*` holds every name in that scope, so an implication such as `write -> use:*` reads
"driving the whole agent opens every capability".
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence


class UnknownPermission(KeyError, ValueError):
    """A permission the vocabulary does not have. A typo must not read as "no", so it is raised."""


class RoleSet:
    def __init__(self, name: str, permissions: Iterable[str], roles: Mapping[str, Iterable[str]], *,
                 implies: Mapping[str, Iterable[str]] | None = None, scopes: Iterable[str] = (),
                 order: Sequence[str] | None = None) -> None:
        self.name = name
        self.permissions = frozenset(permissions)
        self.scopes = frozenset(scopes)
        self._implies = {p: frozenset(v) for p, v in (implies or {}).items()}
        for p, deps in self._implies.items():
            for d in (p, *deps):
                self._check(d, wildcard=True)
        self._roles = {role: self.close(perms) for role, perms in roles.items()}
        #: Weakest first. Defaults to fewest permissions first (a chain for the built-ins).
        self.order = tuple(order) if order is not None else tuple(
            sorted(self._roles, key=lambda r: len(self._roles[r])))
        if set(self.order) != set(self._roles):
            raise ValueError(f"{name}: order must name exactly the roles")
        self._rank = {role: i + 1 for i, role in enumerate(self.order)}

    @classmethod
    def ladder(cls, name: str, permissions: Iterable[str], steps: Sequence[tuple[str, Iterable[str]]], *,
               implies: Mapping[str, Iterable[str]] | None = None, scopes: Iterable[str] = ()) -> RoleSet:
        """Roles weakest first; each holds what the one below holds, plus its own step."""
        roles: dict[str, set[str]] = {}
        held: set[str] = set()
        for role, added in steps:
            held = held | set(added)
            roles[role] = set(held)
        return cls(name, permissions, roles, implies=implies, scopes=scopes, order=[r for r, _ in steps])

    # --- vocabulary -----------------------------------------------------------------

    def _check(self, permission: str, *, wildcard: bool = False) -> str:
        if permission in self.permissions:
            return permission
        scope, sep, rest = permission.partition(":")
        if sep and scope in self.scopes and rest and (wildcard or rest != "*"):
            return permission
        raise UnknownPermission(f"unknown {self.name} permission {permission!r}")

    def close(self, permissions: Iterable[str]) -> frozenset[str]:
        """`permissions` plus everything they imply, transitively."""
        out: set[str] = set()
        todo = [self._check(p, wildcard=True) for p in permissions]
        while todo:
            p = todo.pop()
            if p not in out:
                out.add(p)
                todo.extend(self._implies.get(p, ()))
        return frozenset(out)

    # --- asking -----------------------------------------------------------------------

    @property
    def roles(self) -> tuple[str, ...]:
        """Every role, weakest first."""
        return self.order

    def is_role(self, role: str | None) -> bool:
        return role in self._roles

    def permissions_of(self, role: str | None) -> frozenset[str]:
        """What `role` holds; nothing for None or an unknown role."""
        return self._roles.get(role or "", frozenset())

    def holds(self, permissions: Iterable[str], permission: str) -> bool:
        """Does a person holding `permissions` (any union: several lines, a role) hold `permission`?"""
        self._check(permission)
        held = permissions if isinstance(permissions, (set, frozenset)) else set(permissions)
        if permission in held:
            return True
        scope, sep, _ = permission.partition(":")
        return bool(sep) and f"{scope}:*" in held

    def allows(self, role: str | None, permission: str) -> bool:
        """May someone holding `role` do `permission`? None / an unknown role may do nothing."""
        return self.holds(self.permissions_of(role), permission)

    def rank(self, role: str | None) -> int:
        """0 for no role, else higher is stronger."""
        return self._rank.get(role or "", 0)

    def at_least(self, role: str | None, floor: str) -> bool:
        return self.rank(role) >= self._rank[floor]

    def lowest_with(self, permission: str) -> str | None:
        """The weakest role that holds `permission`, or None."""
        return next((r for r in self.order if self.allows(r, permission)), None)

    def strongest(self, roles: Iterable[str | None]) -> str | None:
        return max((r for r in roles if r in self._rank), key=self.rank, default=None)
