"""Why a machine holds no copy of a workspace vault's key, and who can fix it.

A machine whose person is a member of a workspace vault, and whose runner serves an
agent the vault is attached to, waits for a copy of the vault's key. A keeper (a
runner that holds the key, `canopy vault zk keep`) hands one over within a minute or
two — but only to a machine a person it trusts has ENDORSED (signed for its exact
key, zk-v1 §7). A workspace vault has no endorsements of its own: the keeper accepts
one made for an agent the vault serves (`vault_keeper.work`).

So an approved machine nobody signed for waits forever, and the only person who can
end that cheaply is the machine's own: Settings → Device keys → the machine →
Confirm access (one passkey touch). Running `canopy vault zk keep` anywhere does
nothing for it, and a turn that only sees "this device holds no copy" has no way to
know that (ada fix brief, 2026-11-05: DIM-SA-W175 on the Shared vault).

This module answers the one question every surface asks — the runner's resolve
(`workspace_vaults._layers`), Confirm access (`device_key_out.zk_agents_to_vouch_for`)
and the notice to the machine's person (`zk_notify`) — so they cannot disagree.
"""
from __future__ import annotations

from apps.harness import device_keys
from apps.harness.models import RunnerDeviceKey

from ..models import AgentVaultEndorsement, Vault, VaultKeyWrap
from . import vault_members, zk

#: The machine's person has not signed for it: only they (or an admin of the vault,
#: granting in the browser) can let it in. No keeper will.
UNENDORSED = "unendorsed"
#: Someone signed for it: a keeper passes the key on within minutes, when one is online.
WAITING_FOR_KEEPER = "waiting_for_keeper"
#: Its new decryption key is not approved yet: its person approves it on Device keys.
KEY_NOT_APPROVED = "key_not_approved"
#: Its person is not a member of the vault: an admin of the vault decides.
NOT_A_MEMBER = "not_a_member"


def device_keys_path(key: RunnerDeviceKey) -> str:
    """Settings → Your runners with this machine highlighted (`api_device_keys.approve_path`)."""
    return f"/settings?device_key={key.pk}#your-runners"


def _person(key: RunnerDeviceKey):
    uid = vault_members.person_of_device(key)
    if uid is None:
        return None
    if key.runner.owner_id == uid:
        return key.runner.owner
    return key.approved_by


def vouch_agents(vault: Vault, key: RunnerDeviceKey) -> list:
    """The agents this vault serves that `key`'s person may sign for it under — the ones
    a keeper would accept the signature for: the machine is a current reader of the agent
    and its person a member of the agent's own vault. Oldest-first by slug, stable."""
    uid = vault_members.person_of_device(key)
    out = []
    for agent in sorted(vault_members.attached_agents(vault), key=lambda a: a.slug):
        if uid not in zk.member_ids(agent):
            continue
        if any(k.pk == key.pk for k in zk.eligible_device_keys(agent)):
            out.append(agent)
    return out


def _serves(vault: Vault, key: RunnerDeviceKey) -> bool:
    """Does the vault serve this machine's runner — attached to it, or to an agent it
    runs (`workspace_vaults.device_may_hold` without the membership half)?"""
    from . import workspace_vaults  # noqa: PLC0415 — workspace_vaults imports this module

    if any(r.pk == key.runner_id for r in workspace_vaults.serving_runners(vault)):
        return True
    return any(device_keys.runner_serves_agent(key.runner, a) for a in vault_members.attached_agents(vault))


def _endorsed(vault: Vault, key: RunnerDeviceKey) -> bool:
    """Has a current member of an agent this vault serves signed for this exact key?
    (Whether a given keeper trusts that person is the keeper's call, made on the box.)
    One query for the signatures; membership is computed only for an agent that has one
    — this runs on every credential resolve of a box with a shut layer."""
    if not key.ecdh_jkt:
        return False
    rows = list(AgentVaultEndorsement.objects.filter(
        agent__in=vault_members.attached_agents(vault), kind="device", target=key.ecdh_jkt,
        key_jkt=key.ecdh_jkt).select_related("agent").values_list("agent_id", "endorser_id"))
    if not rows:
        return False
    agents = {a.pk: a for a in vault_members.attached_agents(vault)}
    return any(by in zk.member_ids(agents[aid]) for aid, by in rows if aid in agents)


def why_waiting(vault: Vault, key: RunnerDeviceKey | None) -> dict | None:
    """Why `key` holds no copy of workspace vault `vault`'s current key, and who fixes it:
    ``{reason, machine, person, person_email, fix, link}``. None when it holds one, the
    vault has no key yet, or there is no machine to ask about. Never raises on missing
    rows: an unknown is reported as such, not guessed."""
    if key is None or not vault.kv:
        return None
    if key.status == RunnerDeviceKey.REVOKED or not _serves(vault, key):
        return None   # not a machine this vault would ever hand its key to
    if VaultKeyWrap.objects.filter(vault=vault, kv=vault.kv, kind="device", device_key=key).exists():
        return None
    machine = key.runner.name
    person = _person(key)
    email = getattr(person, "email", "") or ""
    who = email or "its person"
    link = device_keys_path(key)
    base = {"machine": machine, "person_email": email, "link": link}
    if vault_members.person_of_device(key) not in vault_members.member_ids(vault):
        return {**base, "reason": NOT_A_MEMBER,
                "fix": f"{who} is not a member of the {vault.name} vault; an admin of the vault "
                       "decides whether to add them. Running `canopy vault zk keep` will not help."}
    if key.status != RunnerDeviceKey.ACTIVE or not key.ecdh_ready:
        return {**base, "reason": KEY_NOT_APPROVED,
                "fix": f"{machine}'s key is waiting for {who} to approve it: canopy-web Settings → "
                       f"Device keys → {machine} → Approve (passkey). Running `canopy vault zk keep` "
                       "elsewhere will not help."}
    if not _endorsed(vault, key):
        return {**base, "reason": UNENDORSED,
                "fix": f"{machine} is waiting for {who} to confirm it: canopy-web Settings → "
                       f"Device keys → {machine} → Confirm access (passkey). Running "
                       "`canopy vault zk keep` elsewhere will not help."}
    return {**base, "reason": WAITING_FOR_KEEPER,
            "fix": f"{machine} is confirmed; a keeper (a runner that holds the {vault.name} vault's "
                   "key) passes it on within minutes once one is online. If none does, an admin of "
                   "the vault grants it on the vault's page."}
