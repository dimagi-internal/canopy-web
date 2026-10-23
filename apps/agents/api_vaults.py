"""Workspace vaults over REST (`credentials/workspace_vaults.py`; spec
2026-10-22-vault-access-follows-canopy-access-design.md §4).

`/api/workspaces/{slug}/vaults` lists and makes them; `/api/vaults/{vault_id}/…` is one
vault: its members, what it serves, and its zero-knowledge state and writes. The `zk`
routes answer and accept exactly the agent vault's shapes (`ZkStateOut`, `ZkEnableIn`,
`ZkValuesIn`, `ZkWrapsIn`) with the vault's binding id as `agent_id`, so the browser and
`canopy vault zk --vault` encrypt for either with the same code.
"""
from __future__ import annotations

import uuid

from django.contrib.auth import get_user_model
from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError
from apps.api.route_gates import gate
from apps.harness.models import Runner
from apps.harness.runners import can_administer_runner, vault_values_written
from apps.passkeys import stepup
from apps.passkeys.models import UserVaultKeyset
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from .credentials import vault_members, workspace_vaults
from .credentials.zk import ZkError
from .models import Agent, Vault, VaultAttachment, VaultSecret
from .route_refusals import zk_problem
from .schemas import (
    VaultAttachIn,
    VaultCreateIn,
    VaultMemberIn,
    WorkspaceVaultOut,
    ZkEnableIn,
    ZkStateOut,
    ZkValuesIn,
    ZkValuesOut,
    ZkWrapsIn,
)

router = Router(auth=session_auth, tags=["agents"])


def _member_of(request: HttpRequest, slug: str) -> Workspace:
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None or wsvc.membership(request.user, ws) is None:
        raise ProblemError(404, "Not found", detail=f"workspace '{slug}' not found", type_=TYPE_NOT_FOUND)
    return ws


def _vault(request: HttpRequest, vault_id: uuid.UUID, *, manage: bool = False) -> Vault:
    """A workspace vault in a workspace the caller belongs to (else 404); `manage=True`
    also needs the caller to be its owner or admin, and a passkey step-up from a browser."""
    v = Vault.objects.filter(pk=vault_id, kind=Vault.WORKSPACE).select_related("workspace").first()
    if v is None or wsvc.membership(request.user, v.workspace) is None:
        raise ProblemError(404, "Not found", detail="no such vault", type_=TYPE_NOT_FOUND)
    if manage:
        if not vault_members.is_admin(vault_members.role_of(v, request.user)):
            raise HttpError(403, "this action requires someone who manages this vault")
        if stepup.is_browser(request):
            stepup.require_recent_step_up(request)
    return v


def _out(v: Vault, user) -> WorkspaceVaultOut:
    ms = vault_members.members(v)
    keyed = set(UserVaultKeyset.objects.filter(user_id__in=list(ms)).values_list("user_id", flat=True))
    rank = {"owner": 0, "admin": 1, "member": 2}
    rows = sorted(ms.values(), key=lambda m: (rank[m.role], (m.user.email or "").lower()))
    held, keepers = workspace_vaults.copies(v)
    return WorkspaceVaultOut(
        vault_id=str(v.pk), workspace=v.workspace_id, name=v.name, kv=v.kv,
        secrets=sorted(VaultSecret.objects.filter(vault=v).values_list("name", flat=True)),
        attachments=[{"id": a.pk, "agent": a.agent.slug if a.agent_id else None, "workspace": a.workspace_id,
                      "runner": str(a.runner_id) if a.runner_id else None,
                      "runner_name": a.runner.name if a.runner_id else None}
                     for a in VaultAttachment.objects.filter(vault=v).select_related("agent", "runner")],
        members=[{"user_id": m.user.pk, "email": m.user.email or "", "role": m.role, "sources": m.sources,
                  "has_vault_key": m.user.pk in keyed, "has_copy": m.user.pk in held} for m in rows],
        you=(ms[user.pk].role if user.is_authenticated and user.pk in ms else None),
        holders={"has_key": bool(v.kv), "people": sorted(ms[u].user.email or "" for u in held),
                 "keepers": keepers})


def _problem(exc) -> ProblemError:
    if isinstance(exc, vault_members.VaultMembershipError):
        return ProblemError(exc.status, "Vault", detail=exc.detail,
                            type_=TYPE_CONFLICT if exc.status == 409 else TYPE_VALIDATION,
                            members={"error": exc.code})
    return zk_problem(exc)


# ---- the workspace's vaults ----------------------------------------------------------------

@router.get("/workspaces/{slug}/vaults", response=list[WorkspaceVaultOut],
            summary="This workspace's vaults, with their members and what they serve")
@gate("member")
def list_workspace_vaults(request: HttpRequest, slug: str) -> list[WorkspaceVaultOut]:
    """Names, members (with why each is one), attachments and secret NAMES — never a value."""
    ws = _member_of(request, slug)
    return [_out(v, request.user) for v in Vault.objects.filter(workspace=ws, kind=Vault.WORKSPACE)]


@router.post("/workspaces/{slug}/vaults", response=WorkspaceVaultOut,
             summary="Make a workspace vault")
@gate("own", setting=True)
def create_workspace_vault(request: HttpRequest, slug: str, payload: VaultCreateIn) -> WorkspaceVaultOut:
    """`{name}`. Starts with no key (kv 0) and two links: the workspace's owners
    own it, and everyone with full access to an agent it is attached to is a member."""
    ws = _member_of(request, slug)
    if not perms.can(request.user, ws, perms.OWN):
        raise HttpError(403, "making a workspace vault requires the owner role")
    try:
        v = workspace_vaults.create(ws, payload.name, by=request.user)
    except ZkError as exc:
        raise _problem(exc) from exc
    return _out(v, request.user)


# ---- one vault: members and what it serves -------------------------------------------------

@router.get("/vaults/{vault_id}", response=WorkspaceVaultOut, summary="One workspace vault")
@gate("member")
def get_workspace_vault(request: HttpRequest, vault_id: uuid.UUID) -> WorkspaceVaultOut:
    """Its members (and why), what it serves, and the names of its secrets."""
    return _out(_vault(request, vault_id), request.user)


@router.post("/vaults/{vault_id}/members", response=WorkspaceVaultOut, summary="Add a person to a vault by name")
@gate("vault-admin", setting=True)  # browser: + step-up; token allowed
def add_workspace_vault_member(request: HttpRequest, vault_id: uuid.UUID, payload: VaultMemberIn):
    """`{email, role}` — a direct membership. The person must be in the vault's workspace."""
    v = _vault(request, vault_id, manage=True)
    user = get_user_model().objects.filter(email__iexact=payload.email.strip()).first()
    if user is None:
        raise ProblemError(422, "Vault", detail=f"No canopy user has the address {payload.email}.",
                           type_=TYPE_VALIDATION, members={"error": "vault_no_such_user"})
    try:
        vault_members.add_member(v, user, role=payload.role, by=request.user)
    except vault_members.VaultMembershipError as exc:
        raise _problem(exc) from exc
    return _out(v, request.user)


@router.delete("/vaults/{vault_id}/members/{user_id}", response=WorkspaceVaultOut,
               summary="Remove a person's direct membership of a vault")
@gate("vault-admin", setting=True)  # browser: + step-up; token allowed
def remove_workspace_vault_member(request: HttpRequest, vault_id: uuid.UUID, user_id: int):
    """Only the DIRECT membership; a person who is then no longer a member is
    refused the vault at once — canopy-web hands them, and their machines, no
    copy of its key and none of its values."""
    v = _vault(request, vault_id, manage=True)
    try:
        vault_members.remove_member(v, user_id)
    except vault_members.VaultMembershipError as exc:
        raise _problem(exc) from exc
    return _out(v, request.user)


@router.post("/vaults/{vault_id}/attachments", response=WorkspaceVaultOut,
             summary="Serve an agent, every agent in a workspace and below, or a cloud runner from a vault")
@gate("vault-admin", setting=True)  # browser: + step-up; token allowed
def attach_workspace_vault(request: HttpRequest, vault_id: uuid.UUID, payload: VaultAttachIn):
    """`{agent}`, `{workspace}` (the vault's own workspace or one below it) or `{runner}`:
    a cloud runner whose Claude credentials the vault then holds. A runner needs the
    caller to administer it too, and must share an isolation boundary with every runner
    the vault already serves (422 `vault_crosses_isolation`). Attaching makes the box
    eligible for a copy of the key; a holder still has to grant it one."""
    v = _vault(request, vault_id, manage=True)
    agent = Agent.objects.filter(slug=payload.agent).first() if payload.agent else None
    ws = Workspace.objects.filter(slug=payload.workspace).first() if payload.workspace else None
    runner = _administered_runner(request, payload.runner) if payload.runner else None
    if (payload.agent and agent is None) or (payload.workspace and ws is None):
        raise ProblemError(404, "Not found", detail="no such agent or workspace", type_=TYPE_NOT_FOUND)
    try:
        workspace_vaults.attach(v, agent=agent, workspace=ws, runner=runner, by=request.user)
    except ZkError as exc:
        raise _problem(exc) from exc
    return _out(v, request.user)


def _administered_runner(request: HttpRequest, runner_id: str) -> Runner:
    try:
        pk = uuid.UUID(str(runner_id))
    except ValueError:
        pk = None
    runner = Runner.objects.filter(pk=pk).exclude(status=Runner.RETIRED).first() if pk else None
    if runner is None or not can_administer_runner(request.user, runner):
        raise ProblemError(404, "Not found", detail="no such runner you administer", type_=TYPE_NOT_FOUND)
    return runner


@router.delete("/vaults/{vault_id}/attachments/{attachment_id}", response=WorkspaceVaultOut,
               summary="Stop serving an agent or a workspace from a vault")
@gate("vault-admin", setting=True)  # browser: + step-up; token allowed
def detach_workspace_vault(request: HttpRequest, vault_id: uuid.UUID, attachment_id: int):
    v = _vault(request, vault_id, manage=True)
    workspace_vaults.detach(v, attachment_id)
    return _out(v, request.user)


# ---- one vault: zero-knowledge (the agent vault's shapes) ----------------------------------

@router.get("/vaults/{vault_id}/zk", response=ZkStateOut, summary="A workspace vault's zero-knowledge state")
@gate("vault-admin")
def get_workspace_vault_zk(request: HttpRequest, vault_id: uuid.UUID, values: bool = False) -> ZkStateOut:
    """As `GET /api/agents/{slug}/vault/zk`: kv, wraps, pending targets, and with
    `?values=true` the `zk1:` ciphertexts. `agent_id` is the vault's binding id."""
    v = _vault(request, vault_id)
    if not vault_members.is_admin(vault_members.role_of(v, request.user)):
        raise HttpError(403, "this action requires someone who manages this vault")
    return ZkStateOut(**workspace_vaults.state(v, include_values=values))


@router.post("/vaults/{vault_id}/zk/enable", response=ZkStateOut, summary="Make a workspace vault's first key")
@gate("vault-admin", setting=True)  # browser: + step-up; token allowed
def enable_workspace_vault_zk(request: HttpRequest, vault_id: uuid.UUID, payload: ZkEnableIn) -> ZkStateOut:
    """`{kv: 1, wraps, values}` — a wrap the caller can open and a way back are required."""
    v = _vault(request, vault_id, manage=True)
    try:
        workspace_vaults.enable(v, kv=payload.kv, wraps=payload.wraps, values=payload.values, user=request.user)
    except ZkError as exc:
        raise _problem(exc) from exc
    v.refresh_from_db()
    return ZkStateOut(**workspace_vaults.state(v))


@router.put("/vaults/{vault_id}/zk/values", response=ZkValuesOut, summary="Set a workspace vault's values")
@gate("vault-admin", setting=True)  # browser: + step-up; token allowed
def set_workspace_vault_values(request: HttpRequest, vault_id: uuid.UUID, payload: ZkValuesIn) -> ZkValuesOut:
    """Non-clobbering upsert of `zk1:` ciphertext at the current kv."""
    v = _vault(request, vault_id, manage=True)
    try:
        names = workspace_vaults.set_values(v, kv=payload.kv, values=payload.values, user=request.user)
    except ZkError as exc:
        raise _problem(exc) from exc
    # A runner's own credential, sealed here by a person: the copy canopy-web kept goes,
    # and the box reads the vault again.
    vault_values_written(v, names)
    return ZkValuesOut(kv=payload.kv, names=names)


@router.post("/vaults/{vault_id}/zk/wraps", response=ZkStateOut, summary="Grant copies of a workspace vault's key")
@gate("vault-admin", setting=True)  # browser: + step-up; token allowed
def add_workspace_vault_wraps(request: HttpRequest, vault_id: uuid.UUID, payload: ZkWrapsIn) -> ZkStateOut:
    """`{kv, wraps}` to pending members and machines (see the vault's `pending`)."""
    v = _vault(request, vault_id, manage=True)
    try:
        workspace_vaults.add_wraps(v, kv=payload.kv, wraps=payload.wraps, user=request.user)
    except ZkError as exc:
        raise _problem(exc) from exc
    return ZkStateOut(**workspace_vaults.state(v))


@router.delete("/vaults/{vault_id}/secrets/{name}", response=WorkspaceVaultOut,
               summary="Delete one secret from a workspace vault")
@gate("vault-admin", setting=True)  # browser: + step-up; token allowed
def delete_workspace_vault_secret(request: HttpRequest, vault_id: uuid.UUID, name: str):
    v = _vault(request, vault_id, manage=True)
    workspace_vaults.remove_secret(v, name)
    return _out(v, request.user)
