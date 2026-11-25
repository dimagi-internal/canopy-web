"""Tell the vault keepers there is something new to keep, so they ask only then.

A runner runs its keeper (`canopy vault zk keep`, which reads
`GET /runners/{id}/vault-keeper`) when the keeper version in its `/sync` answer
moved (`runner_sync.bump_keepers`), and otherwise only on a slow reconcile. So every
write that can give a keeper something new to do rings the boxes that may keep that
vault, in the same transaction as the write:

* someone newly waiting for a vault key: `zk_notify.sweep` records a notice the first
  time it sees each pending target, whatever made it pending (a new member, a newly
  approved machine, a rotation), and rings then;
* an endorsement a keeper checks a waiting machine against (`zk_endorse`);
* the keepers' record of whose key was trusted (`vault_keeper.store_pins`);
* a credential delegated to an agent, which its machines re-seal to each other
  (`delegations`).

Over-ringing costs one keeper pass on each box rung; a missed ring costs a wait until
the box's reconcile. The rows stay the truth: the keeper always reads the whole of
its work, never a diff.
"""
from __future__ import annotations

from apps.harness import runner_sync
from apps.harness.models import RunnerAssignment, RunnerDeviceKey

from ..models import AgentVaultWrap, Vault, VaultKeyWrap


def keepers_of(*, agent=None, vault=None) -> set:
    """The runners that may keep `agent`'s vault (or the workspace `vault`): every box
    with an active machine that holds a copy of its key at any kv, and, for an agent,
    every box that runs it (it holds the agent's delegated credentials). With neither,
    every box that holds any vault key."""
    if vault is not None and vault.kind == Vault.AGENT:
        agent, vault = vault.agent, None
    if agent is not None:
        ids = set(AgentVaultWrap.objects.filter(kind=AgentVaultWrap.DEVICE, agent=agent,
                                                device_key__status=RunnerDeviceKey.ACTIVE)
                  .values_list("device_key__runner_id", flat=True))
        return ids | set(RunnerAssignment.objects.filter(agent=agent).values_list("runner_id", flat=True))
    if vault is not None:
        return set(VaultKeyWrap.objects.filter(kind="device", vault=vault,
                                               device_key__status=RunnerDeviceKey.ACTIVE)
                   .values_list("device_key__runner_id", flat=True))
    return (set(AgentVaultWrap.objects.filter(kind=AgentVaultWrap.DEVICE, device_key__status=RunnerDeviceKey.ACTIVE)
                .values_list("device_key__runner_id", flat=True))
            | set(VaultKeyWrap.objects.filter(kind="device", device_key__status=RunnerDeviceKey.ACTIVE)
                  .values_list("device_key__runner_id", flat=True)))


def ring(*, agent=None, vault=None) -> None:
    """Move the keeper version of every box `keepers_of` names. Call it inside the
    transaction that wrote the work."""
    runner_sync.bump_keepers(keepers_of(agent=agent, vault=vault))
