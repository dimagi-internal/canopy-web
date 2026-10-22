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

from .credentials import vault_keeper, zk
from .route_refusals import zk_problem
from .schemas import VaultKeeperGrantOut, VaultKeeperOut, ZkWrapsIn

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
