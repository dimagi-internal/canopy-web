"""/api/harness/runners/{runner_id}/vault-keeper — the keeper's two calls
(`credentials/vault_keeper.py`; spec 2026-10-22-vault-access-follows-canopy-access-design.md).

Under the runner's own path, so the box's runner token reaches them
(`apps/harness/runner_auth.py`), and in the agents app because what they answer is
the agents' vaults. Mounted at `/harness` by the composition root.
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.harness.api import runner_or_404

from .credentials import vault_keeper, vault_members, zk
from .models import Agent, Vault
from .route_refusals import zk_problem
from .schemas import VaultKeeperGrantOut, VaultKeeperOut, VaultKeeperPinsIn, VaultKeeperPinsOut, ZkWrapsIn

router = Router(auth=session_auth, tags=["agents"])


@router.get("/runners/{runner_id}/vault-keeper", response=VaultKeeperOut,
            summary="Who is waiting for a vault key this runner holds")
@gate("runner")
def get_vault_keeper_work(request: HttpRequest, runner_id: uuid.UUID) -> VaultKeeperOut:
    """For every zero-knowledge agent whose CURRENT vault key this box holds and
    someone is waiting for: its own wrap, the pending targets (vault members, and
    machines whose person is a member), and the endorsements to check them against.
    The box decides whom to trust; canopy-web only lists."""

    runner = runner_or_404(request, runner_id)
    return VaultKeeperOut(agents=vault_keeper.work(runner))


@router.post("/runners/{runner_id}/vault-keeper/{slug}/wraps", response=VaultKeeperGrantOut,
             summary="A keeper's copies of an agent's vault key, for whoever was waiting")
@gate("runner", setting=True)  # the box's runner token, or its owner
def post_vault_keeper_wraps(request: HttpRequest, runner_id: uuid.UUID, slug: str,
                            payload: ZkWrapsIn) -> VaultKeeperGrantOut:
    """`{kv, wraps}`. Refused (403 `zk_not_a_holder`) unless this box holds the
    agent's current vault key, and (409 `zk_not_pending`) for any target that is not
    waiting — a keeper only adds copies, never replaces one."""

    runner = runner_or_404(request, runner_id)
    try:
        n = vault_keeper.grant(runner, slug, kv=payload.kv, wraps=payload.wraps)
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc
    return VaultKeeperGrantOut(granted=n)


@router.post("/runners/{runner_id}/vault-keeper/vaults/{vault_id}/wraps", response=VaultKeeperGrantOut,
             summary="A keeper's copies of a workspace vault's key, for whoever was waiting")
@gate("runner", setting=True)  # the box's runner token, or its owner
def post_vault_keeper_workspace_wraps(request: HttpRequest, runner_id: uuid.UUID, vault_id: uuid.UUID,
                                      payload: ZkWrapsIn) -> VaultKeeperGrantOut:
    """As for an agent: refused unless this box holds the vault's current key, and
    for any target that is not waiting."""

    runner = runner_or_404(request, runner_id)
    try:
        n = vault_keeper.grant_workspace(runner, vault_id, kv=payload.kv, wraps=payload.wraps)
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc
    return VaultKeeperGrantOut(granted=n)


def _put_pins(request, runner_id, vault, payload: VaultKeeperPinsIn) -> VaultKeeperPinsOut:
    runner = runner_or_404(request, runner_id)
    if vault is None:
        raise zk_problem(zk.ZkError(404, "no_such_vault", "no such vault"))
    try:
        out = vault_keeper.put_pins(runner, vault, kv=payload.kv, expected_version=payload.expected_version,
                                    value=payload.value)
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc
    return VaultKeeperPinsOut(**out)


@router.put("/runners/{runner_id}/vault-keeper/{slug}/pins", response=VaultKeeperPinsOut,
            summary="A keeper's shared record of whose key it trusted, for an agent's vault")
@gate("runner", setting=True)  # the box's runner token, or its owner
def put_vault_keeper_pins(request: HttpRequest, runner_id: uuid.UUID, slug: str,
                          payload: VaultKeeperPinsIn) -> VaultKeeperPinsOut:
    """`{kv, expected_version, value}`: the record, encrypted under the vault key
    (canopy-web cannot read it). Only a box holding the current key may write it,
    over the version it read (409 `keeper_pins_stale`: read again and merge)."""

    agent = Agent.objects.filter(slug=slug).first()
    return _put_pins(request, runner_id, vault_members.vault_for_agent(agent) if agent else None, payload)


@router.put("/runners/{runner_id}/vault-keeper/vaults/{vault_id}/pins", response=VaultKeeperPinsOut,
            summary="A keeper's shared record of whose key it trusted, for a workspace vault")
@gate("runner", setting=True)  # the box's runner token, or its owner
def put_vault_keeper_workspace_pins(request: HttpRequest, runner_id: uuid.UUID, vault_id: uuid.UUID,
                                    payload: VaultKeeperPinsIn) -> VaultKeeperPinsOut:
    """As for an agent's vault."""

    return _put_pins(request, runner_id, Vault.objects.filter(pk=vault_id, kind=Vault.WORKSPACE).first(),
                     payload)
