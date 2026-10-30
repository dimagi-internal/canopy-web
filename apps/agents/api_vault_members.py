"""/api/agents/{slug}/vault/members and /vault/agent-link — who may open an agent's vault, and why.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.errors import (
    TYPE_CONFLICT,
    TYPE_VALIDATION,
    ProblemError,
)
from apps.api.route_gates import gate
from apps.passkeys.models import UserVaultKeyset

from .credentials import (
    vault_members,
    zk,
)
from .schemas import (
    VaultAgentLinkIn,
    VaultMemberIn,
    VaultMembersOut,
)
from .vault_gates import agent_vault

router = Router(auth=session_auth, tags=["agents"])

# ---- vault membership (spec 2026-10-22-vault-access-follows-canopy-access-design.md) ------
# Membership is the vault's own (direct members + links); writes are for its
# managers, and from a browser need a passkey step-up like every vault write.


def _vault_out(vault, user) -> VaultMembersOut:
    ms = vault_members.members(vault)
    keyed = set(UserVaultKeyset.objects.filter(user_id__in=list(ms)).values_list("user_id", flat=True))
    link = vault.links.filter(kind="agent", agent=vault.agent).first() if vault.agent_id else None
    rank = {"owner": 0, "admin": 1, "member": 2}
    rows = sorted(ms.values(), key=lambda m: (rank[m.role], (m.user.email or "").lower()))
    held, keepers = zk.copies(vault.agent)
    return VaultMembersOut(
        vault_id=str(vault.pk), kind=vault.kind, name=vault.name,
        agent_link={"linked": link is not None, "role": link.role if link else "member"},
        members=[{"user_id": m.user.pk, "email": m.user.email or "", "role": m.role, "sources": m.sources,
                  "has_vault_key": m.user.pk in keyed, "has_copy": m.user.pk in held} for m in rows],
        you=(ms[user.pk].role if user.is_authenticated and user.pk in ms else None),
        holders={"has_key": bool(vault.agent.zk_kv),
                 "people": vault_members.granters(ms, held), "keepers": keepers})


def _membership_problem(exc: vault_members.VaultMembershipError) -> ProblemError:
    return ProblemError(exc.status, "Vault membership", detail=exc.detail,
                        type_=TYPE_CONFLICT if exc.status == 409 else TYPE_VALIDATION, members={"error": exc.code})


@router.get("/{slug}/vault/members", response=VaultMembersOut,
            summary="Who may open the agent's vault, and why")
@gate("member")
def get_agent_vault_members(request: HttpRequest, slug: str) -> VaultMembersOut:
    """Every member of the agent's vault with their role (`owner`, `admin` or `member`, the agent's roles) and
    every source of their membership: `direct` (added by name) or a link
    (`agent:<slug>` — full access to the agent). `you` is the caller's role."""

    _, vault = agent_vault(request, slug, manage=False)
    return _vault_out(vault, request.user)


@router.post("/{slug}/vault/members", response=VaultMembersOut,
             summary="Add a person to the agent's vault by name")
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def add_agent_vault_member(request: HttpRequest, slug: str, payload: VaultMemberIn) -> VaultMembersOut:
    """`{email, role}` — a direct membership, kept whatever happens to the
    agent's access list. The person must be a member of the agent's workspace."""

    _, vault = agent_vault(request, slug, manage=True)
    user = get_user_model().objects.filter(email__iexact=payload.email.strip()).first()
    if user is None:
        raise ProblemError(422, "Vault membership", detail=f"No canopy user has the address {payload.email}.",
                           type_=TYPE_VALIDATION, members={"error": "vault_no_such_user"})
    try:
        vault_members.add_member(vault, user, role=payload.role, by=request.user)
    except vault_members.VaultMembershipError as exc:
        raise _membership_problem(exc) from exc
    return _vault_out(vault, request.user)


@router.delete("/{slug}/vault/members/{user_id}", response=VaultMembersOut,
               summary="Remove a person's direct membership of the agent's vault")
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def remove_agent_vault_member(request: HttpRequest, slug: str, user_id: int) -> VaultMembersOut:
    """Removes only the DIRECT membership. Membership through a link stays until
    the person leaves the linked set (e.g. loses full access to the agent). A
    person who is no longer a member is refused the vault at once: canopy-web
    hands them, and their machines, no copy of its key and none of its values."""

    _, vault = agent_vault(request, slug, manage=True)
    try:
        vault_members.remove_member(vault, user_id)
    except vault_members.VaultMembershipError as exc:
        raise _membership_problem(exc) from exc
    return _vault_out(vault, request.user)


@router.put("/{slug}/vault/agent-link", response=VaultMembersOut,
            summary="Link the agent's vault to the agent's access list, or unlink it")
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def set_agent_vault_link(request: HttpRequest, slug: str, payload: VaultAgentLinkIn) -> VaultMembersOut:
    """`{linked, role}`. Linked, everyone with full access to the agent is a
    member: its owner as `owner`, its admins as `admin`, everyone else as `role`.
    Unlinked, only direct members remain. Refused (409 `vault_needs_an_admin`) when
    it would leave the vault with no owner or admin."""

    agent, vault = agent_vault(request, slug, manage=True)
    try:
        vault_members.set_agent_link(vault, agent, linked=payload.linked, role=payload.role, by=request.user)
    except vault_members.VaultMembershipError as exc:
        raise _membership_problem(exc) from exc
    return _vault_out(vault, request.user)


