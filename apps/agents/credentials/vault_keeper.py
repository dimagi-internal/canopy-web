"""The keeper: a runner that holds an agent's vault key passes it on, with nobody asked
(spec 2026-10-22-vault-access-follows-canopy-access-design.md §1).

Under zero-knowledge only a holder of the vault key can give a copy to someone new.
A runner that holds one (its device wrap at the current kv) is a holder, and it is
online, so it can do what the "Give access" email asked a person to do. The box does
the crypto and the TRUST CHECK (canopy `vault zk keep`: a member's key is pinned on
first sight, a machine needs an endorsement from a person it trusts); canopy-web only
says who is waiting and stores what comes back.

What canopy-web enforces here, so a keeper adds no authority of its own:

* it lists, and accepts wraps for, only agents whose CURRENT vault key this box holds;
* a wrap is accepted only for a target that is pending — eligible now
  (`zk.pending_targets`: a vault member, or a machine whose person is one) and holding
  no copy yet — so a keeper can never replace a working copy with a bad one;
* everything else is `zk.add_wraps`, the same check a person's grant goes through.

**The keepers' record** (`Vault.keeper_pins_zk`). Which key a keeper trusted for each
person is kept in the vault itself, encrypted under the vault key (`zk1`, bound to
the name `~keeper-pins` and the vault's kv), so every keeper — a brand-new cloud box
included — refuses a person whose key has changed, and canopy-web, which cannot read
or forge it, cannot swap a key unseen. A keeper reads it with its work and writes it
back with `put_pins` (compare-and-swap on `keeper_pins_version`). A record made under
an older vault key is unreadable after a rotation; keepers start a new one.

**Who keeps.** Only a machine whose person is an OWNER or ADMIN of the vault is a keeper
(`vault_members.device_works_for_an_admin`) — the people who may change who is in it.

**Announced.** A keeper's grant to a PERSON is emailed to the vault's owners and admins
(`_announce`), so nobody gets a vault without someone who manages it hearing of it.
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction

from apps.harness import device_keys
from apps.harness.models import RunnerDeviceKey

from ..models import Agent, AgentVaultWrap, Vault, VaultKeyWrap
from . import vault_members, workspace_vaults, zk, zk_endorse

logger = logging.getLogger(__name__)


def _held(runner) -> list[AgentVaultWrap]:
    """This box's device wraps at each zero-knowledge agent's current kv — only while
    the box may pass that key on NOW: it serves the agent and its person is an owner
    or admin of the vault. Holding a copy is never itself permission to pass one on;
    a member's machine reads the vault but is no keeper."""
    return [w for w in (AgentVaultWrap.objects
                        .filter(kind=AgentVaultWrap.DEVICE, device_key__runner=runner,
                                device_key__status=RunnerDeviceKey.ACTIVE, agent__zk_enabled=True)
                        .select_related("agent", "device_key__runner"))
            if w.kv == w.agent.zk_kv
            and vault_members.device_works_for_an_admin(vault_members.vault_for_agent(w.agent), w.device_key)
            and device_keys.runner_serves_agent(runner, w.agent)]


def work(runner) -> list[dict]:
    """For each agent this box holds the current vault key of AND someone is waiting
    for: what a keeper needs to grant — its own wrap, the pending targets, and the
    endorsements it checks them against."""
    out = []
    for w in _held(runner):
        agent = w.agent
        pending = zk.pending_targets(agent)
        if not pending:
            continue
        base = f"/api/harness/runners/{runner.pk}/vault-keeper/{agent.slug}"
        out.append({"agent": agent.slug, "agent_id": str(agent.pk), "kv": agent.zk_kv,
                    "device_key": str(w.device_key_id), "wrap": w.wrap, "pending": pending,
                    "endorsements": [{**e, "agent_id": str(agent.pk)} for e in zk_endorse.endorsements(agent)],
                    "pins": pins_of(vault_members.vault_for_agent(agent)), "pins_path": f"{base}/pins",
                    "grant_path": f"{base}/wraps"})
    for w in _held_workspace(runner):
        v = w.vault
        pending = workspace_vaults.pending_targets(v)
        if not pending:
            continue
        # A workspace vault has no endorsements of its own: a machine is vouched for by
        # the endorsement its approver made for an agent this vault serves, verified
        # against THAT agent's id (each row carries it).
        ends = [{**e, "agent_id": str(a.pk)} for a in vault_members.attached_agents(v)
                for e in zk_endorse.endorsements(a)]
        base = f"/api/harness/runners/{runner.pk}/vault-keeper/vaults/{v.pk}"
        out.append({"agent": v.name, "agent_id": workspace_vaults.binding_id(v), "kv": v.kv,
                    "device_key": str(w.device_key_id), "wrap": w.wrap, "pending": pending,
                    "endorsements": ends, "vault_id": str(v.pk),
                    "pins": pins_of(v), "pins_path": f"{base}/pins", "grant_path": f"{base}/wraps"})
    return out


def _held_workspace(runner) -> list[VaultKeyWrap]:
    """This box's device wraps at each workspace vault's current kv — only while it
    may hold that key now (`workspace_vaults.device_may_hold`) and its person is an
    owner or admin of the vault."""
    return [w for w in (VaultKeyWrap.objects
                        .filter(kind="device", device_key__runner=runner,
                                device_key__status=RunnerDeviceKey.ACTIVE, vault__kv__gt=0)
                        .select_related("vault", "device_key__runner"))
            if w.kv == w.vault.kv and workspace_vaults.device_may_hold(w.vault, w.device_key)
            and vault_members.device_works_for_an_admin(w.vault, w.device_key)]


def grant_workspace(runner, vault_id, *, kv: int, wraps: list) -> int:
    """`grant`, for a workspace vault this box holds the current key of."""
    v = Vault.objects.filter(pk=vault_id, kind=Vault.WORKSPACE).first()
    if v is None or not any(w.vault_id == v.pk for w in _held_workspace(runner)):
        raise zk.ZkError(403, "zk_not_a_holder", "this runner holds no copy of that vault's current key")
    pending = {(t["kind"], t["target"]) for t in workspace_vaults.pending_targets(v)}
    for i, item in enumerate(wraps):
        if (item.kind, str(item.target)) not in pending:
            raise zk.ZkError(409, "zk_not_pending",
                             f"wraps[{i}]: {item.kind} {item.target} is not waiting for a copy — a "
                             "keeper only adds copies, never replaces one")
    n = workspace_vaults.add_wraps(v, kv=kv, wraps=wraps, user=runner.owner, only_new=True)
    _announce(v, wraps, runner)
    return n


def grant(runner, slug: str, *, kv: int, wraps: list) -> int:
    """Store a keeper's wraps for `slug`. Refused unless this box holds the current
    vault key and every target is pending (eligible, with no copy yet)."""
    agent = Agent.objects.filter(slug=slug).first()
    if agent is None or not any(w.agent_id == agent.pk for w in _held(runner)):
        raise zk.ZkError(403, "zk_not_a_holder",
                         f"this runner holds no copy of {slug}'s current vault key")
    pending = {(t["kind"], t["target"]) for t in zk.pending_targets(agent)}
    for i, item in enumerate(wraps):
        if (item.kind, str(item.target)) not in pending:
            raise zk.ZkError(409, "zk_not_pending",
                             f"wraps[{i}]: {item.kind} {item.target} is not waiting for a copy — a "
                             "keeper only adds copies, never replaces one")
    n = zk.add_wraps(agent, kv=kv, wraps=wraps, user=runner.owner, only_new=True)
    _announce(vault_members.vault_for_agent(agent), wraps, runner)
    return n


# ---- the keepers' record ---------------------------------------------------------------------

PINS_NAME = "~keeper-pins"


def pins_of(vault: Vault | None) -> dict:
    """What a keeper needs to read the record: its ciphertext, the kv it was made
    under, and the version to write back against."""
    if vault is None:
        return {"value": "", "kv": 0, "version": 0}
    return {"value": vault.keeper_pins_zk, "kv": vault.keeper_pins_kv, "version": vault.keeper_pins_version}


def _current_kv(vault: Vault) -> int:
    return vault.agent.zk_kv if vault.kind == Vault.AGENT else vault.kv


def put_pins(runner, vault: Vault, *, kv: int, expected_version: int, value: str) -> dict:
    """Store a keeper's new record. Only a box that holds the vault's current key may,
    at that kv, and only over the version it read (409 `keeper_pins_stale` otherwise:
    read again, merge, retry)."""
    held = ({w.agent_id for w in _held(runner)} if vault.kind == Vault.AGENT
            else {w.vault_id for w in _held_workspace(runner)})
    if (vault.agent_id if vault.kind == Vault.AGENT else vault.pk) not in held:
        raise zk.ZkError(403, "zk_not_a_holder", "this runner holds no copy of that vault's current key")
    return store_pins(vault, kv=kv, expected_version=expected_version, value=value)


def store_pins(vault: Vault, *, kv: int, expected_version: int, value: str) -> dict:
    """Store a new record at the vault's current kv, over the version the writer read.
    A keeper writes it (`put_pins`), and so does an owner's or admin's browser when it
    grants someone: that grant is the confirmation of the key it granted to."""
    zk.check_value_zk(value, PINS_NAME)
    with transaction.atomic():
        v = Vault.objects.select_for_update(of=("self",)).select_related("agent").get(pk=vault.pk)
        if kv != _current_kv(v):
            raise zk.ZkError(409, "zk_kv_mismatch", f"the vault key is at kv {_current_kv(v)}, not {kv}")
        if expected_version != v.keeper_pins_version:
            raise zk.ZkError(409, "keeper_pins_stale",
                             f"the record is at version {v.keeper_pins_version}, not {expected_version}: "
                             "read it again and merge")
        v.keeper_pins_zk, v.keeper_pins_kv = value, kv
        v.keeper_pins_version += 1
        v.save(update_fields=["keeper_pins_zk", "keeper_pins_kv", "keeper_pins_version"])
    return pins_of(v)


# ---- announcing a keeper's grant -------------------------------------------------------------

def _announce(vault: Vault | None, wraps: list, runner) -> None:
    """Email the vault's owners and admins who a keeper just gave the key to. Never raises:
    the copies are stored either way."""
    try:
        people = [str(w.target) for w in wraps if w.kind == "member"]
        if vault is None or not people:
            return
        users = {str(u.pk): u for u in get_user_model().objects.filter(pk__in=people)}
        names = ", ".join(users[p].email for p in people if p in users)
        admins = [m.user for m in vault_members.members(vault).values()
                    if vault_members.is_admin(m.role) and str(m.user.pk) not in people]
        to = sorted({getattr(u, "email", "") for u in admins} - {""})
        if not to:
            return
        what = vault.agent.slug if vault.kind == Vault.AGENT else f"the {vault.name} vault"
        subject = f"Canopy vault · {names} now has {what}'s key"
        body = (f"The keeper on {runner.name} gave {names} a copy of {what}'s vault key, because "
                f"they are now a member of the vault.\n\nIf that is not right, remove them from the "
                f"vault and change its key.\n\nYou are getting this because you are an owner or admin "
                f"of {what}'s vault.\n")
        send_mail(subject, body, None, to, fail_silently=True)
    except Exception:  # noqa: BLE001 — a failed notice must not fail the grant
        logger.exception("vault keeper: announcing a grant failed")

