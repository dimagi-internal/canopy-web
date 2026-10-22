"""Who may open a vault: its members (spec 2026-10-22-vault-access-follows-canopy-access-design.md).

A vault's membership is first-class and is the union of two sources, each kept
with the member so a page can always say WHY someone can open it:

* **direct** — a `VaultMember` row: a person added by name;
* **linked** — a `VaultLink` rule, re-read on every question so it follows the
  set it names: everyone with full access to an agent (`access.roster`; the
  agent's owner and admins come in as `manage`) and everyone holding an active
  license to use it locally, or the owners / admins of a workspace (inherited
  down the tree as those roles are).

A person's role is the highest any source gives them: `manage` (see and edit
values, change membership) above `use` (open the values from their own browser
and machines).

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
    AgentVaultEndorsement,
    AgentVaultWrap,
    Vault,
    VaultAttachment,
    VaultLink,
    VaultMember,
)

logger = logging.getLogger(__name__)

MANAGE, USE = VaultMember.MANAGE, VaultMember.USE
_RANK = {USE: 0, MANAGE: 1}


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
            VaultLink.objects.create(vault=v, kind=VaultLink.AGENT, agent=agent, role=USE)
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
            role = MANAGE if row["agent_role"] in (access.OWNER, access.ADMIN) else link.role
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
            _add(out, m.user, link.role, source)


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


def members(vault: Vault | None) -> dict[int, Membership]:
    """Every member of `vault`, by user id, with their role and every source."""

    out: dict[int, Membership] = {}
    if vault is None:
        return out
    for row in VaultMember.objects.filter(vault=vault).select_related("user"):
        _add(out, row.user, row.role, "direct")
    for link in VaultLink.objects.filter(vault=vault).select_related("agent__workspace", "workspace", "vault"):
        _linked(link, out)
    return out


def member_ids(vault: Vault | None) -> set[int]:
    return set(members(vault))


def role_of(vault: Vault | None, user) -> str | None:
    """`manage`, `use`, or None (not a member)."""

    if user is None or not getattr(user, "is_authenticated", False):
        return None
    m = members(vault).get(user.pk)
    return m.role if m is not None else None


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


def _check_role(role: str) -> None:
    if role not in _RANK:
        raise VaultMembershipError(422, "vault_bad_role", "role must be manage or use")


def _require_a_manager(vault: Vault) -> None:
    """Every change keeps someone who can manage the vault, or nobody could ever
    change it again — checked inside the change's transaction, after it."""

    if not any(m.role == MANAGE for m in members(vault).values()):
        raise VaultMembershipError(409, "vault_needs_a_manager",
                                   "this would leave the vault with nobody who can manage it")


def add_member(vault: Vault, user, *, role: str, by) -> VaultMember:
    """Add `user` by name (or change their direct role). They must be a member
    of the vault's workspace: a vault cannot reach past its tenant."""
    _check_role(role)
    if wsvc.membership(user, vault.workspace) is None:
        raise VaultMembershipError(422, "vault_not_in_workspace",
                                   f"{user.email} is not a member of {vault.workspace.slug}")
    with transaction.atomic():
        row, _ = VaultMember.objects.update_or_create(vault=vault, user=user,
                                                      defaults={"role": role, "added_by": by})
        _require_a_manager(vault)
    return row


def remove_member(vault: Vault, user_id: int) -> bool:
    """Remove a DIRECT membership. Membership a link gives is not touched: it
    goes when the person leaves the linked set, or the link is removed."""

    with transaction.atomic():
        deleted, _ = VaultMember.objects.filter(vault=vault, user_id=user_id).delete()
        _require_a_manager(vault)
    return bool(deleted)


def set_agent_link(vault: Vault, agent: Agent, *, linked: bool, role: str = USE, by=None) -> VaultLink | None:
    """Link the vault to `agent` (everyone with full access to it becomes a
    member; its owner and admins as manage, everyone else as `role`), or unlink."""

    _check_role(role)
    with transaction.atomic():
        if not linked:
            VaultLink.objects.filter(vault=vault, kind=VaultLink.AGENT, agent=agent).delete()
            _require_a_manager(vault)
            return None
        link, _ = VaultLink.objects.update_or_create(vault=vault, kind=VaultLink.AGENT, agent=agent,
                                                     defaults={"role": role, "created_by": by})
    return link


# ---- removal: key copies follow membership --------------------------------------------------

@dataclass
class Reconciled:
    member_wraps: int = 0
    device_wraps: int = 0
    endorsements: int = 0

    @property
    def total(self) -> int:
        return self.member_wraps + self.device_wraps + self.endorsements


def reconcile(agent: Agent) -> Reconciled:
    """Delete every copy of the agent's vault key, and every endorsement, that no
    longer has a member of the agent's vault behind it: a person who left, or a
    machine whose person left. canopy-web then stops handing them the vault's
    ciphertext (`credential_resolve` refuses a non-member's machine as well).

    The vault key is NOT changed: as in 1Password, removal stops the server
    serving the vault. A person who kept a copy while they were a member is the
    case for "Change the key" and rotating the outside secrets themselves."""
    out = Reconciled()
    ids = agent_member_ids(agent)
    with transaction.atomic():
        out.member_wraps, _ = (AgentVaultWrap.objects.filter(agent=agent, kind=AgentVaultWrap.MEMBER)
                               .exclude(user_id__in=ids).delete())
        stale = [w.pk for w in AgentVaultWrap.objects.filter(agent=agent, kind=AgentVaultWrap.DEVICE)
                 .select_related("device_key__runner") if person_of_device(w.device_key) not in ids]
        out.device_wraps, _ = AgentVaultWrap.objects.filter(pk__in=stale).delete()
        ends = AgentVaultEndorsement.objects.filter(agent=agent)
        gone = list(ends.exclude(endorser_id__in=ids).values_list("pk", flat=True))
        gone += [e.pk for e in ends.filter(kind="member") if not e.target.isdigit() or int(e.target) not in ids]
        out.endorsements, _ = AgentVaultEndorsement.objects.filter(pk__in=gone).delete()
    return out


def reconcile_all() -> int:
    """`reconcile` every zero-knowledge agent. Returns how many rows went."""
    n = 0
    for agent in Agent.objects.filter(zk_enabled=True, workspace__isnull=False):
        try:
            r = reconcile(agent)
        except Exception:  # noqa: BLE001 — one agent's failure must not stop the rest
            logger.exception("vault reconcile failed for %s", agent.slug)
            continue
        if r.total:
            logger.info("vault reconcile %s: %s", agent.slug, r)
        n += r.total
    return n


def device_works_for_a_member(agent: Agent, device_key) -> bool:
    """Does `device_key` work for a member of the agent's vault?"""
    return person_of_device(device_key) in agent_member_ids(agent)
