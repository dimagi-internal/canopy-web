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

from .credentials import accounts, delegations, vault_keeper, vault_members, zk
from .models import Agent, Vault
from .route_refusals import zk_problem
from .schemas import (
    DelegatedResealIn,
    DelegatedResealOut,
    RunnerDelegatedCheckIn,
    RunnerDelegatedCheckOut,
    VaultKeeperGrantOut,
    VaultKeeperOut,
    VaultKeeperPinsIn,
    VaultKeeperPinsOut,
    ZkWrapsIn,
)

router = Router(auth=session_auth, tags=["agents"])


@router.get("/runners/{runner_id}/vault-keeper", response=VaultKeeperOut,
            summary="Who is waiting for a vault key this runner holds")
@gate("runner", step_up="runner protocol: a box's own token; the values are ciphertext only machines open")
def get_vault_keeper_work(request: HttpRequest, runner_id: uuid.UUID) -> VaultKeeperOut:
    """For every zero-knowledge agent whose CURRENT vault key this box holds and
    someone is waiting for: its own wrap, the pending targets (vault members, and
    machines whose person is a member), and the endorsements to check them against.
    The box decides whom to trust; canopy-web only lists."""

    runner = runner_or_404(request, runner_id)
    return VaultKeeperOut(agents=vault_keeper.work(runner), delegated=accounts.keeper_work(runner))


@router.put("/runners/{runner_id}/vault-keeper/{slug}/delegated/{name}", response=DelegatedResealOut,
            summary="A keeper re-seals a delegated credential it holds for a machine that was waiting")
# the box's runner token, or its owner
@gate("runner", setting=True,
      step_up="runner protocol: a box's own token; the value is ciphertext only machines open")
def put_delegated_reseal(request: HttpRequest, runner_id: uuid.UUID, slug: str, name: str,
                         payload: DelegatedResealIn) -> DelegatedResealOut:
    """`{value_zk, expect}`: the credential re-sealed to the machines that run the agent.
    Refused unless this box held it and still runs the agent (403 `zk_not_a_holder`), for
    any recipient that is not one of those machines or the person it came from (422), for
    one that drops a machine that held it (422), and when it changed since the box read it
    (409 `zk_value_changed`)."""

    runner = runner_or_404(request, runner_id)
    agent = Agent.objects.filter(slug=slug).first()
    if agent is None:
        raise zk_problem(zk.ZkError(404, "not_found", "no such agent"))
    try:
        machines = accounts.reseal(agent, name, value_zk=payload.value_zk, expect=payload.expect,
                                   runner=runner)
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc
    return DelegatedResealOut(machines=machines)


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


@router.post("/runners/{runner_id}/delegated-checks", response=RunnerDelegatedCheckOut,
             summary="A machine reports what it found probing a credential delegated to an agent")
# a box that runs the agent, reporting on a credential sealed to it
@gate("runner", "runner-holds-agent", kind="runner-protocol", step_up="runner protocol")
def post_delegated_checks(request: HttpRequest, runner_id: uuid.UUID,
                          payload: RunnerDelegatedCheckIn) -> RunnerDelegatedCheckOut:
    """canopy-web cannot open a credential a person delegated to an agent, so the machines
    that run the agent probe it (for GitHub: may it push to, and open a pull request on,
    the agent's repo) and report here. Recorded on the entry as checked by this machine;
    a failed probe is what tells its person to replace it. Only from a box that runs the
    agent; nothing it reports changes who the entry is sealed to or whose it is."""

    runner = runner_or_404(request, runner_id)
    agent = Agent.objects.filter(slug=payload.agent).first()
    if agent is None or not any(k.runner_id == runner.pk for k in accounts.delegated_devices(agent)):
        raise zk_problem(zk.ZkError(404, "not_found", "no such delegated credential"))
    entry = delegations.record_checks(agent, payload.name, checks=[c.model_dump() for c in payload.checks],
                                      provider_expires_at=payload.provider_expires_at,
                                      error=payload.error, by=runner.name)
    if entry is None:
        raise zk_problem(zk.ZkError(404, "not_found", "no such delegated credential"))
    return RunnerDelegatedCheckOut(label=accounts.label(entry, agent))
