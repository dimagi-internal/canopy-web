"""Which agents a person signs their own machine under, so a zero-knowledge WORKSPACE
vault's keepers pass that machine the vault's key (Settings → Device keys → Confirm access).

A workspace vault has no endorsements of its own: its keepers accept a signature made
under an agent the vault serves (`vault_keeper.work`), from a current member of that
agent's vault. So a machine that runs an agent the vault serves, whose person is a
member of the workspace vault and of that agent's vault, can be let in by its own
person's passkey, with no admin asked. Before this, Confirm access named only agents
whose OWN vault waited on the machine, so a box running only a kv-0 agent its person
owns (DIM-SA-W175 on Shared) was offered nothing and waited forever.

Read for every key on Settings → Device keys, so it is built to cost a fixed handful of
queries per request (`facts.once`), not a scan per key and vault: an earlier version
walked every workspace vault through `why_waiting` for each key and was reverted for
the load it put on vault reads.
"""
from __future__ import annotations

from apps.harness import device_keys
from apps.harness.models import RunnerDeviceKey

from ..models import AgentVaultEndorsement, Vault, VaultKeyWrap
from . import vault_members, zk


def _once(facts, key, fn):
    return facts.once(key, fn) if facts is not None else fn()


def _keyed_vaults(facts) -> list[Vault]:
    return _once(facts, ("ws-vouch", "vaults"),
                 lambda: list(Vault.objects.filter(kind=Vault.WORKSPACE, kv__gte=1).select_related("workspace")))


def _holders(facts, vaults: list[Vault]) -> set[tuple]:
    """(vault, device key) for every device copy at each vault's current kv — one read."""
    def read():
        current = {v.pk: v.kv for v in vaults}
        rows = VaultKeyWrap.objects.filter(vault_id__in=list(current), kind="device").values_list(
            "vault_id", "kv", "device_key_id")
        return {(vid, dk) for vid, kv, dk in rows if current.get(vid) == kv}
    return _once(facts, ("ws-vouch", "holders"), read)


def agents_to_sign_under(person_id: int, key: RunnerDeviceKey, facts=None) -> list:
    """The agents `key`'s person (`person_id`) should sign it under, for every
    zero-knowledge workspace vault that would hand the machine its key but has not and
    for which no current member has signed yet. Each is an agent the vault serves, that
    the machine runs, whose vault the person is a member of: exactly the signatures the
    vault's keepers accept. Whatever the agent's own `zk_kv`, and whether or not the
    person administers it. The person's own signature only: nothing here signs."""
    if not key.ecdh_jkt or key.status == RunnerDeviceKey.REVOKED:
        return []
    vaults = _keyed_vaults(facts)
    if not vaults:
        return []
    held = _holders(facts, vaults)
    out: dict[int, object] = {}
    for vault in vaults:
        if (vault.pk, key.pk) in held:
            continue
        attached = _once(facts, ("ws-vouch", "attached", vault.pk), lambda v=vault: vault_members.attached_agents(v))
        # In memory: which of them this machine runs (the fleet's facts, read once).
        runs = [a for a in attached if device_keys.runner_serves_agent(key.runner, a, facts)]
        if not runs:
            continue
        members = _once(facts, ("ws-vouch", "members", vault.pk), lambda v=vault: vault_members.member_ids(v))
        if person_id not in members:
            continue   # not theirs to let in: the vault's admin decides
        signable = [a for a in runs if person_id in zk.member_ids(a, facts)]
        if not signable or _signed(key, runs, facts):
            continue
        out.update((a.pk, a) for a in signable)
    return sorted(out.values(), key=lambda a: a.slug)


def _signed(key: RunnerDeviceKey, agents: list, facts) -> bool:
    """Has a current member of one of `agents`' vaults already signed for this exact key?
    Then a keeper grants it (or its own line says whom it does not trust): nothing to ask."""
    rows = AgentVaultEndorsement.objects.filter(
        agent_id__in=[a.pk for a in agents], kind="device", target=key.ecdh_jkt, key_jkt=key.ecdh_jkt,
    ).values_list("agent_id", "endorser_id")
    by_agent = {a.pk: a for a in agents}
    return any(uid in zk.member_ids(by_agent[aid], facts) for aid, uid in rows)
