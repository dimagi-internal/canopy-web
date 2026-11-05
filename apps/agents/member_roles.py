"""The vocabulary every first-class membership shares (a vault's, a project's): the roles,
the highest-role-wins merge that remembers why, and who can never be a member. One copy,
so a vault linked to a project and the project itself cannot disagree on what a role means."""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Agent
from .models.members import NamedMember

OWNER, ADMIN, MEMBER = NamedMember.OWNER, NamedMember.ADMIN, NamedMember.MEMBER
_RANK = {MEMBER: 0, ADMIN: 1, OWNER: 2}
#: What a row or a link may give: owner comes only from what the person already holds.
ASSIGNABLE = {ADMIN, MEMBER}
#: The words before the roles matched the agent's; still accepted from older clients.
_OLD_NAMES = {"manage": ADMIN, "use": MEMBER}


class MembershipError(Exception):
    """A refused membership change. `code` is the machine-readable `error`."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def is_admin(role: str | None) -> bool:
    """May change the membership: its owners and admins."""
    return role in (OWNER, ADMIN)


def role_named(role: str) -> str:
    """A role as a client sent it, in today's words (`manage` → `admin`, `use` → `member`)."""
    return _OLD_NAMES.get(role, role)


@dataclass
class Membership:
    user: object
    role: str
    #: Why, in the words of whoever resolved it: "direct", "lead", "agent:<slug>", "project:<address>", …
    sources: list[str] = field(default_factory=list)


def add(out: dict[int, Membership], user, role: str, source: str) -> None:
    """Record that `source` makes `user` a member as `role`; the highest role any source gives stands."""
    m = out.get(user.pk)
    if m is None:
        out[user.pk] = Membership(user, role, [source])
        return
    if _RANK[role] > _RANK[m.role]:
        m.role = role
    if source not in m.sources:
        m.sources.append(source)


def not_a_person(user_ids) -> set[int]:
    """Of `user_ids`, the logins that are not people: an agent's own canopy login
    (`Agent.user`). It is never a member of anything that opens a secret: it holds no
    vault key, and an agent able to change a membership could hand a key to anyone
    (a prompt-injected agent adding a reader)."""
    ids = set(user_ids)
    return set(Agent.objects.filter(user_id__in=ids).values_list("user_id", flat=True))
