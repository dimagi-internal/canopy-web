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
from apps.harness.models import Runner, RunnerDeviceKey
from apps.harness.runners import can_administer_runner, vault_values_written
from apps.passkeys import stepup
from apps.passkeys.models import UserVaultKeyset
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces import vault as shared_vault
from apps.workspaces.models import Workspace
from apps.workspaces.schemas import SharedVaultIn, SharedVaultOut

from .credentials import vault_keeper, vault_members, workspace_vaults
from .credentials.zk import ZkError
from .models import Agent, Vault, VaultAttachment, VaultSecret
from .route_refusals import zk_problem
from .schemas import (
    VaultAttachIn,
    VaultCreateIn,
    VaultKeeperPinsIn,
    VaultKeeperPinsOut,
    VaultMemberIn,
    WorkspaceVaultOut,
    ZkEnableIn,
    ZkRotateIn,
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
    attached = list(VaultAttachment.objects.filter(vault=v).select_related("agent", "runner"))
    # A runner it serves is a member at the hardware level: say where its key lives and
    # whether it holds a copy of the vault's key, is waiting for one, or cannot have one yet.
    runner_ids = [a.runner_id for a in attached if a.runner_id]
    waiting = ({t.get("runner") for t in workspace_vaults.pending_targets(v) if t.get("kind") == "device"}
               if v.kv and runner_ids else set())
    places = dict(RunnerDeviceKey.objects.filter(runner_id__in=runner_ids, status=RunnerDeviceKey.ACTIVE)
                  .exclude(ecdh_jkt="").values_list("runner_id", "ecdh_store"))

    def access(a) -> str:
        if not v.kv:
            return ""
        if a.runner.name in keepers:
            return "has"
        return "waiting" if str(a.runner_id) in waiting else "none"

    return WorkspaceVaultOut(
        vault_id=str(v.pk), workspace=v.workspace_id, name=v.name, kv=v.kv,
        secrets=sorted(VaultSecret.objects.filter(vault=v).values_list("name", flat=True)),
        attachments=[{"id": a.pk, "agent": a.agent.slug if a.agent_id else None, "workspace": a.workspace_id,
                      "runner": str(a.runner_id) if a.runner_id else None,
                      "runner_name": a.runner.name if a.runner_id else None,
                      "access": access(a) if a.runner_id else "",
                      "key_place": places.get(a.runner_id, "") if a.runner_id else ""}
                     for a in attached],
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

def _owner_of(request: HttpRequest, slug: str) -> Workspace:
    """A workspace the caller OWNS: 404 for a non-member (no probing roles), 403 below owner."""
    ws = _member_of(request, slug)
    if not perms.can(request.user, ws, perms.OWN):
        raise HttpError(403, f"this requires the {perms.MINIMUM_ROLE[perms.OWN]} role or above")
    return ws


# The shared 1Password vault (`Workspace.op_vault`, `workspaces/vault.py`) sits with
# the workspace vaults because setting its key needs a passkey step-up, and the
# workspaces app is below passkeys (ARCHITECTURE.md § Layers inside the framework).
@router.get("/workspaces/{slug}/shared-vault", response=SharedVaultOut,
            summary="This tenant's shared 1Password vault (masked — never the key)")
@gate("own")
def get_shared_vault(request: HttpRequest, slug: str) -> SharedVaultOut:
    """Owner-only, matching the rest of workspace administration.

    Reading it is masked, so the restriction is not about the value — it is that
    which vault a tenant reads is administrative, and an editor acts *within* a
    tenant rather than over its credential configuration.
    """
    return shared_vault.shared_vault_status(_owner_of(request, slug))


@router.put("/workspaces/{slug}/shared-vault", response=SharedVaultOut,
            summary="Set the shared vault + its service-account token (write-only)")
@gate("own", setting=True, step_up=True)
def set_shared_vault(request: HttpRequest, slug: str, payload: SharedVaultIn) -> SharedVaultOut:
    """The key here must be scoped to the SHARED vault and nothing else.

    A key that also reads the per-agent vaults would undo the reason those are
    split (Agent.op_vault, 2026-09-06): what this one unlocks is shared by
    definition, so its breadth costs nothing, and that is only true while it
    stays narrow. Nothing here can enforce that — 1Password grants it — so it is
    stated where whoever sets it will read it. From a browser it needs a fresh
    passkey step-up (403 `step_up_required` otherwise); a token write does not.
    """
    ws = _owner_of(request, slug)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    return shared_vault.set_shared_vault(ws, vault=payload.vault, service_key=payload.service_key)


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
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
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
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
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
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
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
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def detach_workspace_vault(request: HttpRequest, vault_id: uuid.UUID, attachment_id: int):
    v = _vault(request, vault_id, manage=True)
    workspace_vaults.detach(v, attachment_id)
    return _out(v, request.user)


# ---- one vault: zero-knowledge (the agent vault's shapes) ----------------------------------

@router.get("/vaults/{vault_id}/zk", response=ZkStateOut, summary="A workspace vault's zero-knowledge state")
@gate("vault-admin", step_up="zk1: ciphertext only, which canopy-web cannot open")
def get_workspace_vault_zk(request: HttpRequest, vault_id: uuid.UUID, values: bool = False) -> ZkStateOut:
    """As `GET /api/agents/{slug}/vault/zk`: kv, wraps, pending targets, and with
    `?values=true` the `zk1:` ciphertexts. `agent_id` is the vault's binding id."""
    v = _vault(request, vault_id)
    if not vault_members.is_admin(vault_members.role_of(v, request.user)):
        raise HttpError(403, "this action requires someone who manages this vault")
    return ZkStateOut(**workspace_vaults.state(v, include_values=values), pins=vault_keeper.pins_of(v))


@router.put("/vaults/{vault_id}/zk/pins", response=VaultKeeperPinsOut,
            summary="Record the keys a vault owner or admin just granted to, for the keepers")
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def put_workspace_vault_pins(request: HttpRequest, vault_id: uuid.UUID,
                             payload: VaultKeeperPinsIn) -> VaultKeeperPinsOut:
    """As `PUT /api/agents/{slug}/vault/zk/pins`, for a workspace vault."""
    v = _vault(request, vault_id, manage=True)
    try:
        return VaultKeeperPinsOut(**vault_keeper.store_pins(
            v, kv=payload.kv, expected_version=payload.expected_version, value=payload.value))
    except ZkError as exc:
        raise zk_problem(exc) from exc


@router.post("/vaults/{vault_id}/zk/enable", response=ZkStateOut, summary="Make a workspace vault's first key")
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def enable_workspace_vault_zk(request: HttpRequest, vault_id: uuid.UUID, payload: ZkEnableIn) -> ZkStateOut:
    """`{kv: 1, wraps, values}` — a wrap the caller can open and a way back are required."""
    v = _vault(request, vault_id, manage=True)
    try:
        workspace_vaults.enable(v, kv=payload.kv, wraps=payload.wraps, values=payload.values, user=request.user)
    except ZkError as exc:
        raise _problem(exc) from exc
    v.refresh_from_db()
    return ZkStateOut(**workspace_vaults.state(v))


@router.post("/vaults/{vault_id}/zk/rotate", response=ZkStateOut,
             summary="Rotate a workspace vault's key: replace every copy and value atomically")
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def rotate_workspace_vault_zk(request: HttpRequest, vault_id: uuid.UUID, payload: ZkRotateIn) -> ZkStateOut:
    """As `POST /api/agents/{slug}/vault/zk/rotate`: `{new_kv: kv + 1, wraps, values}`.
    Every member or machine holding a copy now that still qualifies must get one at
    `new_kv` (422 `zk_rewrap_required` otherwise); one removed since is simply left
    out. A way back and a copy the caller can open are required, and `values` must
    cover every stored secret."""
    v = _vault(request, vault_id, manage=True)
    try:
        workspace_vaults.rotate(v, new_kv=payload.new_kv, wraps=payload.wraps, values=payload.values,
                                user=request.user)
    except ZkError as exc:
        raise _problem(exc) from exc
    v.refresh_from_db()
    # Every value is under the new key: a box serving from this vault reads it again.
    vault_values_written(v, sorted(VaultSecret.objects.filter(vault=v).values_list("name", flat=True)))
    return ZkStateOut(**workspace_vaults.state(v))


@router.put("/vaults/{vault_id}/zk/values", response=ZkValuesOut, summary="Set a workspace vault's values")
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
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
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
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
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def delete_workspace_vault_secret(request: HttpRequest, vault_id: uuid.UUID, name: str):
    v = _vault(request, vault_id, manage=True)
    workspace_vaults.remove_secret(v, name)
    return _out(v, request.user)
