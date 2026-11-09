"""What a device key shows: its row, and the agents it opens or is waiting on (the non-route half of api_device_keys.py, so `apps/agents` need not import a route module).
"""
from __future__ import annotations

from apps.agents.models import Agent
from apps.passkeys.models import UserVaultKeyset

from . import device_keys
from .models import RunnerDeviceKey
from .schemas import (
    DeviceKeyAgentOut,
    DeviceKeyVouchOut,
    RunnerDeviceKeyOut,
)


def key_out(key: RunnerDeviceKey, *, can_manage: bool) -> RunnerDeviceKeyOut:
    return RunnerDeviceKeyOut(
        id=key.pk, runner=key.runner_id, runner_name=key.runner.name,
        runner_host=key.runner.host, runner_kind=key.runner.kind, jkt=key.jkt,
        kind=key.kind, label=key.label, role_arn=key.role_arn, status=key.status,
        ecdh_jkt=key.ecdh_jkt, ecdh_public_jwk=key.ecdh_public_jwk or {}, ecdh_store=key.ecdh_store,
        reapproval=key.status == RunnerDeviceKey.PENDING and key.approved_at is not None,
        ecdh_waiting=key.ecdh_waiting,
        created_at=key.created_at,
        approved_at=key.approved_at,
        approved_by_email=(key.approved_by.email if key.approved_by_id else ""),
        revoked_at=key.revoked_at, last_used_at=key.last_used_at, can_manage=can_manage,
    )


def zk_agents_waiting_on(user, key: RunnerDeviceKey, *, assume_active: bool = False, agents=None, facts=None) -> list[str]:
    """Zero-knowledge agents `user` administers that this (now active) key
    should be granted the vault key of — the "Grant" offer after approving.
    `assume_active`: as if it were approved (the list, for a pending key).

    An agent born zero-knowledge (kv 0) has no vault key to grant, only values
    sealed to whoever could read it when each was saved. It is listed when one
    of those values was not sealed to this key: the browser then creates the
    agent's vault key in the same passkey touch (opening the sealed values with
    the person's own key), so a machine approved later can read them too —
    nobody has to "finish setting up" an agent by hand."""
    if not key.ecdh_jkt or key.status == RunnerDeviceKey.REVOKED or not (assume_active or key.ecdh_ready):
        return []
    from apps.agents.credentials import zk

    if agents is None:
        agents = Agent.objects.select_related("workspace", "owner")
    return sorted(
        agent.slug for agent in agents
        if agent.is_admin(user) and device_keys.runner_serves_agent(key.runner, agent, facts)
        and zk.device_waits(agent, key)
    )


def zk_agents_to_vouch_for(user, key: RunnerDeviceKey, *, assume_active: bool = False, agents=None,
                           facts=None) -> list[DeviceKeyVouchOut]:
    """Zero-knowledge agents whose vault `user` is a member of WITHOUT managing it, that
    this key — `user`'s own machine — waits on a copy of the vault key for; and the agents
    a zero-knowledge WORKSPACE vault waiting on it accepts their signature under
    (`workspace_vouch`).

    A member cannot wrap the key (only its managers grant), but their
    approval signs the machine: a keeper then wraps to it, with nobody else asked.
    Without this, a member who approved their own box would be told nothing and the
    agent's owner would get a "no vault-key wrap" alert. The agents `zk_agents_waiting_on` lists are left out: their
    manager grants directly."""
    if not key.ecdh_jkt or key.status == RunnerDeviceKey.REVOKED or not (assume_active or key.ecdh_ready):
        return []
    from apps.agents.credentials import vault_members, workspace_vouch, zk  # noqa: PLC0415 — cycle via apps.agents.credentials

    # Whose machine it is: the runner's owner, else whoever approves it — the caller,
    # for a key they are about to approve.
    person = key.runner.owner_id or key.approved_by_id or (user.pk if assume_active else None)
    if person != user.pk or not UserVaultKeyset.objects.filter(user=user).exists():
        return []
    out = []
    if agents is None:
        agents = Agent.objects.select_related("workspace", "owner")
    for agent in (a for a in agents if a.zk_kv > 0):
        vault = vault_members.vault_for_agent(agent)
        role = vault_members.role_of(vault, user)
        if role is None or (agent.is_admin(user) and vault_members.is_admin(role)):
            continue
        if device_keys.runner_serves_agent(key.runner, agent, facts) and zk.device_waits(agent, key):
            out.append(DeviceKeyVouchOut(slug=agent.slug, agent_id=str(agent.pk)))
    # And for each zero-knowledge workspace vault waiting on the machine: the agents it
    # serves that the person signs under, whatever their kv and whoever administers them
    # (DIM-SA-W175 ran only a kv-0 agent its person owns, so nothing above named it).
    have = {a.slug for a in out}
    out += [DeviceKeyVouchOut(slug=a.slug, agent_id=str(a.pk))
            for a in workspace_vouch.agents_to_sign_under(user.pk, key, facts) if a.slug not in have]
    return sorted(out, key=lambda a: a.slug)


def agents_key_opens(key: RunnerDeviceKey, agents: list, facts=None) -> list[DeviceKeyAgentOut]:
    """Of `agents`, the ones this key can open right now: its decryption key is
    approved, the box serves the agent, and nothing of the agent's is still
    waiting on it (`zk.device_waits`) — "can open Hal, Eva" on Settings."""
    if not key.ecdh_ready:
        return []
    from apps.agents.credentials import zk

    return [DeviceKeyAgentOut(slug=a.slug, name=a.name or a.slug) for a in agents
            if device_keys.runner_serves_agent(key.runner, a, facts) and not zk.device_waits(a, key)]
