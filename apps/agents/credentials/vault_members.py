"""Who may open a vault: its members (spec 2026-10-22-vault-access-follows-canopy-access-design.md).

A vault's membership is first-class and is the union of two sources, each kept
with the member so a page can always say WHY someone can open it:

* **direct** — a `VaultMember` row: a person added by name;
* **linked** — a `VaultLink` rule, re-read on every question so it follows the
  set it names: everyone with full access to an agent (`access.roster`; the
  agent's owner comes in as `owner`, its admins as `admin`) and everyone holding an active
  license to use it locally, or the owners / admins of a workspace (inherited
  down the tree as those roles are).

A person's role is the highest any source gives them, in the agent's words:
`owner` and `admin` (see and edit values, change membership) above `member` (open
the values from their own browser and machines).

The machine layer asks the same question of a device's PERSON
(`person_of_device`): a machine may hold a vault's key only while the person it
works for is a member, so approving a machine never grants by itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Q

from apps.workspaces import services as wsvc

from .. import access
from ..models import (
    Agent,
    AgentLicense,
    Vault,
    VaultAttachment,
    VaultLink,
    VaultMember,
)

logger = logging.getLogger(__name__)

OWNER, ADMIN, MEMBER = VaultMember.OWNER, VaultMember.ADMIN, VaultMember.MEMBER
_RANK = {MEMBER: 0, ADMIN: 1, OWNER: 2}
#: What a row or a link may give: owner comes only from owning the agent or workspace.
_ASSIGNABLE = {ADMIN, MEMBER}
#: The words before the roles matched the agent's; still accepted from older clients.
_OLD_NAMES = {"manage": ADMIN, "use": MEMBER}


def is_admin(role: str | None) -> bool:
    """May see and edit values and change membership: the vault's owners and admins."""
    return role in (OWNER, ADMIN)


def role_named(role: str) -> str:
    """A role as a client sent it, in today's words (`manage` → `admin`, `use` → `member`)."""
    return _OLD_NAMES.get(role, role)


@dataclass
class Membership:
    user: object
    role: str
    #: Why: "direct", "agent:<slug>", "workspace_owners:<slug>", "workspace_admins:<slug>".
    sources: list[str] = field(default_factory=list)


def vault_for_agent(agent: Agent) -> Vault | None:
    """The agent's vault, made (linked to the agent) the first time it is asked
    for. None for an agent with no workspace: it has no members to give one."""

    if not agent.workspace_id:
        return None
    v = Vault.objects.filter(agent=agent).first()
    if v is not None:
        return v
    with transaction.atomic():
        v, made = Vault.objects.get_or_create(
            agent=agent, defaults={"workspace_id": agent.workspace_id, "kind": Vault.AGENT, "name": agent.slug})
        if made:
            VaultLink.objects.create(vault=v, kind=VaultLink.AGENT, agent=agent, role=MEMBER)
    return v


def _add(out: dict[int, Membership], user, role: str, source: str) -> None:
    m = out.get(user.pk)
    if m is None:
        out[user.pk] = Membership(user, role, [source])
        return
    if _RANK[role] > _RANK[m.role]:
        m.role = role
    if source not in m.sources:
        m.sources.append(source)


def _linked(link: VaultLink, out: dict[int, Membership]) -> None:
    if link.kind == VaultLink.AGENT:
        source = f"agent:{link.agent.slug}"
        for row in access.roster(link.agent):
            if row["access"] != access.FULL:
                continue
            role = {access.OWNER: OWNER, access.ADMIN: ADMIN}.get(row["agent_role"], link.role)
            _add(out, _UserRef(row["user_id"], row["email"]), role, source)
        # A person licensed to use the agent locally (`AgentLicense`, canopy-web#1704)
        # runs its tools on their own machine, so the link counts them too — an
        # admin may approve a license for someone the caller list alone would not.
        for lic in AgentLicense.objects.filter(agent=link.agent, status=AgentLicense.ACTIVE).select_related("user"):
            _add(out, lic.user, link.role, f"license:{link.agent.slug}")
        return
    if link.kind == VaultLink.ATTACHED:
        # Everyone with full access to an agent this workspace vault serves, as the
        # link's role: an agent's admins run that agent, they do not manage the
        # workspace's vault (its owners do, through their own link).
        for agent in attached_agents(link.vault):
            for row in access.roster(agent):
                if row["access"] == access.FULL:
                    _add(out, _UserRef(row["user_id"], row["email"]), link.role, f"attached:{agent.slug}")
        return
    wanted = {"owner"} if link.kind == VaultLink.WORKSPACE_OWNERS else {"owner", "admin"}
    source = f"{link.kind}:{link.workspace.slug}"
    for m in wsvc.effective_memberships(link.workspace):
        if m.role in wanted:
            # A workspace's owners own its vault, as an agent's owner owns the agent's.
            _add(out, m.user, OWNER if m.role == "owner" else link.role, source)


def attached_agents(vault: Vault) -> list[Agent]:
    """Every agent a WORKSPACE vault serves: its direct attachments, and every agent in
    an attached workspace or below it."""
    rows = list(VaultAttachment.objects.filter(vault=vault))
    ids = {r.agent_id for r in rows if r.agent_id}
    roots = {r.workspace_id for r in rows if r.workspace_id}
    tree = roots | wsvc.descendant_slugs(roots) if roots else set()
    return list(Agent.objects.filter(Q(pk__in=ids) | Q(workspace_id__in=tree)).select_related("workspace"))


@dataclass(frozen=True)
class _UserRef:
    """What a roster row gives: enough to key and show a member without a query."""
    pk: int
    email: str


def not_a_person(user_ids) -> set[int]:
    """Of `user_ids`, the logins that are not people: an agent's own canopy login
    (`Agent.user`). It is never a vault member — it holds no vault key, and an agent
    able to change its own vault's membership could hand its key to anyone (a
    prompt-injected agent adding a reader)."""
    ids = set(user_ids)
    return set(Agent.objects.filter(user_id__in=ids).values_list("user_id", flat=True))


def members(vault: Vault | None) -> dict[int, Membership]:
    """Every member of `vault`, by user id, with their role and every source. People
    only (`not_a_person`): an agent's login is never one."""

    out: dict[int, Membership] = {}
    if vault is None:
        return out
    direct = list(VaultMember.objects.filter(vault=vault).select_related("user"))
    # A direct member counts only while they are still in the vault's workspace
    # (directly or inherited): leaving it, or the vault moving, ends it.
    roles = wsvc.member_roles_of(row.user for row in direct) if direct else {}
    for row in direct:
        if vault.workspace.slug in roles.get(row.user_id, {}):
            _add(out, row.user, row.role, "direct")
    for link in VaultLink.objects.filter(vault=vault).select_related("agent__workspace", "workspace", "vault"):
        _linked(link, out)
    for uid in not_a_person(out):
        out.pop(uid, None)
    return out


def member_ids(vault: Vault | None) -> set[int]:
    return set(members(vault))


def role_of(vault: Vault | None, user) -> str | None:
    """`owner`, `admin`, `member`, or None (not a member)."""

    if user is None or not getattr(user, "is_authenticated", False):
        return None
    m = members(vault).get(user.pk)
    return m.role if m is not None else None


def granters(ms: dict[int, Membership], held) -> list[str]:
    """The emails of the people in `held` (holding a copy of the key) who can grant it:
    the vault's owners and admins — a member's copy opens the vault but lets nobody in."""
    return sorted(ms[u].user.email or "" for u in held if u in ms and is_admin(ms[u].role))


def agent_member_ids(agent: Agent) -> set[int]:
    """The members of the agent's own vault — who the agent's wraps may go to."""

    return member_ids(vault_for_agent(agent))


def person_of_device(device_key) -> int | None:
    """The user a machine works for: its runner's owner, else whoever approved
    its key. None when neither is known — such a machine works for nobody."""

    runner = device_key.runner
    return runner.owner_id or device_key.approved_by_id


# ---- changing membership ----------------------------------------------------------------

class VaultMembershipError(Exception):
    """A refused membership change. `code` is the machine-readable `error`."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def _check_role(role: str) -> str:
    role = role_named(role)
    if role not in _ASSIGNABLE:
        raise VaultMembershipError(422, "vault_bad_role",
                                   "role must be admin or member (owner comes from owning the agent or workspace)")
    return role


def _lock(vault: Vault) -> Vault:
    """Hold the vault's row for this transaction, so two membership writes
    cannot each see the other's admin and together leave none."""
    return Vault.objects.select_for_update().get(pk=vault.pk)


def _require_an_admin(vault: Vault) -> None:
    """Every change keeps an owner or admin, or nobody could ever change the vault
    again — checked inside the change's transaction, after it."""

    if not any(is_admin(m.role) for m in members(vault).values()):
        raise VaultMembershipError(409, "vault_needs_an_admin",
                                   "this would leave the vault with no owner or admin")


def add_member(vault: Vault, user, *, role: str, by) -> VaultMember:
    """Add `user` by name (or change their direct role). They must be a member
    of the vault's workspace: a vault cannot reach past its tenant."""
    role = _check_role(role)
    if not_a_person([user.pk]):
        raise VaultMembershipError(422, "vault_not_a_person",
                                   f"{user.email} is an agent's login, not a person")
    if wsvc.membership(user, vault.workspace) is None:
        raise VaultMembershipError(422, "vault_not_in_workspace",
                                   f"{user.email} is not a member of {vault.workspace.slug}")
    with transaction.atomic():
        _lock(vault)
        row, _ = VaultMember.objects.update_or_create(vault=vault, user=user,
                                                      defaults={"role": role, "added_by": by})
        _require_an_admin(vault)
    return row


def remove_member(vault: Vault, user_id: int) -> bool:
    """Remove a DIRECT membership. Membership a link gives is not touched: it
    goes when the person leaves the linked set, or the link is removed."""

    with transaction.atomic():
        _lock(vault)
        deleted, _ = VaultMember.objects.filter(vault=vault, user_id=user_id).delete()
        _require_an_admin(vault)
    return bool(deleted)


def set_agent_link(vault: Vault, agent: Agent, *, linked: bool, role: str = MEMBER, by=None) -> VaultLink | None:
    """Link the vault to `agent` (everyone with full access to it becomes a
    member; its owner as owner, its admins as admin, everyone else as `role`), or unlink."""

    role = _check_role(role)
    with transaction.atomic():
        _lock(vault)
        if not linked:
            VaultLink.objects.filter(vault=vault, kind=VaultLink.AGENT, agent=agent).delete()
            _require_an_admin(vault)
            return None
        link, _ = VaultLink.objects.update_or_create(vault=vault, kind=VaultLink.AGENT, agent=agent,
                                                     defaults={"role": role, "created_by": by})
    return link


# ---- the machine layer, asked on every read --------------------------------------------------

def device_works_for_an_admin(vault: Vault | None, device_key) -> bool:
    """Does `device_key` work for an owner or admin of `vault`? Only such a machine is
    a keeper: the people who may change who is in a vault are the ones whose boxes
    pass its key on, as in 1Password (an admin's client, or the bridge they run)."""
    m = members(vault).get(person_of_device(device_key))
    return m is not None and is_admin(m.role)


def device_works_for_a_member(agent: Agent, device_key) -> bool:
    """Does `device_key` work for a member of the agent's vault?"""
    return person_of_device(device_key) in agent_member_ids(agent)
