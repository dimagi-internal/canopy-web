"""/api/agents/{slug}/vault/zk… — the zero-knowledge vault: its state, the first key, values, grants, pins and rotation.

Formats: `docs/vault/zk-v1/`. canopy-web only shape-checks and stores; the browser and the canopy CLI
do every encryption (`apps/agents/credentials/zk.py`). Who may write: `vault_gates`.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.errors import (
    TYPE_FORBIDDEN,
    ProblemError,
)
from apps.api.route_gates import gate

from . import services
from .credentials import (
    vault_keeper,
    vault_members,
    zk,
)
from .gates import (
    agent_for_admin,
)
from .route_refusals import zk_problem
from .schemas import (
    VaultKeeperPinsIn,
    VaultKeeperPinsOut,
    ZkEnableIn,
    ZkRotateIn,
    ZkStateOut,
    ZkValuesIn,
    ZkValuesOut,
    ZkWrapsIn,
)
from .vault_gates import agent_vault, zk_admin

router = Router(auth=session_auth, tags=["agents"])

# ---- zero-knowledge vault (Canopy Vault Phase 2, canopy-web#1550) ------------------
# Formats: docs/vault/zk-v1/zk_v1_reference.py. canopy-web only shape-checks and
# stores; the browser and the canopy CLI do every encryption (apps/agents/credentials/zk.py).
# Owner/admin only. Writes from a browser need a passkey step-up; a token held
# by an admin may enable, set values, grant and rotate (the canopy CLI acts as
# its device). There is no turning it off: the agent vault has no other mode.


def _zk_state(agent, *, include_values: bool = False) -> ZkStateOut:

    return ZkStateOut(**zk.state(agent, include_values=include_values),
                      pins=vault_keeper.pins_of(vault_members.vault_for_agent(agent)))


@router.get("/{slug}/vault/zk", response=ZkStateOut,
            summary="Zero-knowledge state: kv, wraps, and who still needs one")
# zero-knowledge vault (apps/agents/credentials/zk.py): browser writes need a passkey step-up
# wraps are public; ?values= ciphertext, read-logged
@gate("agent-admin", step_up="zk1: ciphertext only, which canopy-web cannot open")
def get_agent_zk_state(request: HttpRequest, slug: str, values: bool = False) -> ZkStateOut:
    """`pending` lists active device keys (with an ECDH key) on runners this
    agent's resolve accepts, and current agent admins with a vault key, that
    hold no wrap at the current `kv` — each with the public key to wrap to.
    `wraps` are public data. `?values=true` adds the `zk1:` ciphertexts (what
    rotate and turn-off need), and records a read of each."""
    agent = agent_for_admin(request, slug)
    if values and request.user.pk not in vault_members.agent_member_ids(agent):
        # An agent admin who is not a member of its vault (the link was removed)
        # may still hold an old copy of the key: the ciphertext goes to members only.
        raise ProblemError(403, "Not a vault member", type_=TYPE_FORBIDDEN,
                           detail=f"You are not a member of {agent.slug}'s vault; a manager of the vault must add you.",
                           members={"error": "not_a_vault_member"})
    out = _zk_state(agent, include_values=values)
    if values and out.values:
        services.record_credential_reads(agent, list(out.values), user=request.user, via="zk-state")
    return out


@router.post("/{slug}/vault/zk/enable", response=ZkStateOut,
             summary="Turn on zero-knowledge: wraps + every value as ciphertext, atomically")
@gate("agent-admin", "vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def enable_agent_zk(request: HttpRequest, slug: str, payload: ZkEnableIn) -> ZkStateOut:
    """`{kv: 1, wraps: [{kind, target, wrap}], values: [{name, value_zk}]}`.
    Needs exactly one `recovery` wrap and a wrap the caller can open (their
    member wrap, or a device wrap on a runner they own — the CLI enabling as
    its device; the owner's member wrap can be granted later). `values` must cover every stored credential,
    re-encrypted under the new vault key. Errors are problem+json with an `error` code."""

    agent = zk_admin(request, slug)
    try:
        zk.enable(agent, kv=payload.kv, wraps=payload.wraps, values=payload.values, user=request.user)
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc
    agent.refresh_from_db()
    return _zk_state(agent)


@router.put("/{slug}/vault/zk/values", response=ZkValuesOut,
            summary="Set zero-knowledge values (ciphertext) at the current kv")
@gate("agent-admin", "vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def set_agent_zk_values(request: HttpRequest, slug: str, payload: ZkValuesIn) -> ZkValuesOut:
    """Non-clobbering upsert of `{name, value_zk}`. A `kv` other than the
    current one is 409 `zk_kv_mismatch` (the vault key was rotated)."""

    agent = zk_admin(request, slug)
    try:
        names = zk.set_values(agent, kv=payload.kv, values=payload.values, user=request.user)
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc
    # Storing a value is declaring it: the caller is the agent's owner or admin.
    services.declare_credential_names(agent, names)
    return ZkValuesOut(kv=payload.kv, names=names)


@router.post("/{slug}/vault/zk/wraps", response=ZkStateOut,
             summary="Grant: wrap the vault key to pending devices or admins")
@gate("agent-admin", "vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def add_agent_zk_wraps(request: HttpRequest, slug: str, payload: ZkWrapsIn) -> ZkStateOut:
    """`{kv, wraps: [{kind: device|member, target, wrap}]}`. A device target is
    an ACTIVE key with an ECDH key on a runner this agent routes to, working for
    a member of the agent's vault; a member target is a member of the agent's
    vault with a vault key. Replaces that target's
    wrap at this kv. The recovery wrap is set only by enable and rotate."""

    agent = zk_admin(request, slug)
    try:
        zk.add_wraps(agent, kv=payload.kv, wraps=payload.wraps, user=request.user)
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc
    return _zk_state(agent)


@router.put("/{slug}/vault/zk/pins", response=VaultKeeperPinsOut,
            summary="Record the keys a vault owner or admin just granted to, for the keepers")
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def put_agent_zk_pins(request: HttpRequest, slug: str, payload: VaultKeeperPinsIn) -> VaultKeeperPinsOut:
    """`{kv, expected_version, value}`: the keepers' record, re-encrypted under the
    vault key with the people the caller just granted to. Granting someone is how an
    owner or admin confirms their key — a first one, or one they started over with —
    so keepers trust it from then on. 409 `keeper_pins_stale`: read again, merge."""

    _, vault = agent_vault(request, slug, manage=True)
    try:
        return VaultKeeperPinsOut(**vault_keeper.store_pins(
            vault, kv=payload.kv, expected_version=payload.expected_version, value=payload.value))
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc


@router.post("/{slug}/vault/zk/rotate", response=ZkStateOut,
             summary="Rotate the vault key: replace every wrap and value atomically")
@gate("agent-admin", "vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def rotate_agent_zk(request: HttpRequest, slug: str, payload: ZkRotateIn) -> ZkStateOut:
    """`{new_kv: kv + 1, wraps, values}`. Every target holding a wrap now that
    still qualifies must get one at `new_kv` (422 `zk_rewrap_required`
    otherwise); a revoked device or a removed member is simply left out. A fresh
    recovery wrap and a wrap the caller can open are required, and `values`
    must cover every stored credential."""

    agent = zk_admin(request, slug)
    try:
        zk.rotate(agent, new_kv=payload.new_kv, wraps=payload.wraps, values=payload.values,
                  user=request.user)
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc
    agent.refresh_from_db()
    return _zk_state(agent)


