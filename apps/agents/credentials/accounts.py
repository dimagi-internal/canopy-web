"""Whose account a vault entry is: the agent's own, or delegated by a person.

THE RULE: an agent acts as itself — with its own credentials or ones a person
delegated to it — or as you only under an act-as-you permission you granted.

Every vault entry carries the same account fields (`models.VaultEntryAccount`). Code
asks this module, never the columns:

- `is_delegated(entry, agent)`: a person's (or another agent's) account, used by
  `agent`. An entry whose `delegated_by_agent` IS `agent` is that agent's own.
- `delegated_devices(agent, person)`: the only machines `person`'s delegated entry is
  sealed to — the cloud boxes that run the agent's turns, and machines of `person`'s
  own that do. Not the vault's members, not its admins, not another admin's laptop,
  not a license holder's box.
- `serves(entry, agent)`: may a resolve hand it out now. Never refuses on a date.
- `label(entry, agent)`: the one sentence every screen and alert uses.

A delegated value is sealed (`zks1`, zk-v1 §6) to each of `delegated_devices`, and
never under the vault key, so no member's browser and not canopy-web can open it. It
is never "settled" into the vault key either: generic value writes refuse its name
(`refuse_delegated_names`), and it leaves canopy-web only in a resolve's `delegated`
field, to a runner read proved by one of those machines (`resolve_payload`).

Spec: docs/superpowers/specs/2026-10-26-delegated-credentials-in-the-vault-design.md
"""
from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.utils import timezone

from apps.harness import device_keys
from apps.harness.models import Runner, RunnerDeviceKey
from apps.passkeys.models import UserVaultKeyset

from ..models import Agent, AgentCredential, VaultEntryAccount
from ..services import runner_may_hold_agent
from . import authority, vault_engine, zk, zk_endorse, zk_seal
from .zk import ZkError

GITHUB_ENTRY = "github-token"
PROVIDER_NAMES = {"github": "GitHub", "google": "Google", "salesforce": "Salesforce", "anthropic": "Claude", "other": ""}


def is_delegated(entry, agent) -> bool:
    if entry.account != VaultEntryAccount.DELEGATED:
        return False
    return not (entry.delegated_by_agent_id and entry.delegated_by_agent_id == agent.pk)


def delegated_entries(agent):
    """The agent vault's delegated entries (its own, by `delegated_by_agent`, excluded)."""
    return (AgentCredential.objects.filter(agent=agent, account=VaultEntryAccount.DELEGATED)
            .exclude(delegated_by_agent=agent).select_related("delegated_by", "delegated_by_agent"))


def delegated_names(agent) -> set[str]:
    return set(delegated_entries(agent).values_list("name", flat=True))


def refuse_delegated_names(agent, names) -> None:
    """A generic value write (a member's browser, the CLI's settle, `PUT /credentials`)
    never touches a delegated entry: it would put a person's credential where the
    vault's members could read it. Only its person replaces it (`delegations.give_github`),
    and only a machine that opened it re-seals it to a new machine (`reseal`)."""
    clash = sorted(delegated_names(agent) & {str(n).strip() for n in names})
    if clash:
        raise ZkError(409, "delegated_entry",
                      f"{', '.join(clash)}: a credential a person delegated to {agent.slug}; only they "
                      "can replace it, and nobody can read it")


# ---- the machines that may open a delegated entry --------------------------------

def delegated_devices(agent, person, facts=None) -> list:
    """The machines a credential `person` delegated to `agent` is sealed to: ACTIVE device
    keys with an APPROVED ECDH key, on a runner that serves `agent` and whose owner is one
    of its admins (`runner_may_hold_agent`), and that is a cloud box or `person`'s own
    (`may_hold_for`). A person's computer without the runner app runs no turn, so it is
    not one."""

    keys = (device_keys.keys_for_agent(agent, facts)
            .filter(status=RunnerDeviceKey.ACTIVE, ecdh_approved_at__isnull=False)
            .exclude(ecdh_jkt="").exclude(runner__kind=Runner.COMPUTER)
            .select_related("runner__owner"))
    return [k for k in keys if may_hold_for(k.runner, person) and runner_may_hold_agent(k.runner, agent)]


def may_hold_for(runner, person) -> bool:
    """May `runner` hold a credential `person` delegated? A cloud box, or a machine of
    their own. A laptop's device key sits in its owner's home directory, so its owner
    can open anything sealed to it: another admin's laptop would hand them `person`'s
    token. Being an admin of the agent is trust to run it, not to read its people's
    credentials."""
    return runner.kind == Runner.CLOUD or (person is not None and runner.owner_id == person.pk)


def entry_devices(entry, agent, facts=None) -> list:
    """`delegated_devices` for the person `entry` came from."""
    return delegated_devices(agent, entry.delegated_by, facts)


def recipients(agent, person) -> list[tuple[str, str, dict]]:
    """Who a delegated value is sealed to: each machine that runs `agent`, and the person
    it came from when they have a vault key (so a browser of theirs could re-seal it)."""

    out = [("device", k.ecdh_jkt, k.ecdh_public_jwk) for k in delegated_devices(agent, person)]
    ks = UserVaultKeyset.objects.filter(user=person).first() if person is not None else None
    if ks is not None:
        out.append(("member", str(ks.user_id), ks.public_jwk))
    return out


def seal(agent, name: str, plaintext: str, person) -> tuple[str, list[str]]:
    """`(zks1 value, names of the machines that can open it)`. Refused (409) when no
    machine that runs `agent` can open one yet: a credential nobody can use is a
    credential nobody notices is missing."""
    to = recipients(agent, person)
    machines = sorted({k.runner.name for k in delegated_devices(agent, person)})
    if not machines:
        raise ZkError(409, "no_machine_can_open",
                      f"no machine that runs {agent.slug} can open a credential of yours yet: a cloud "
                      f"runner that serves {agent.slug}, or a runner of your own, needs an approved "
                      "device key")
    return zk_seal.seal_to(plaintext, agent_id=str(agent.pk), name=name, kv=agent.zk_kv,
                           recipients=to), machines


# ---- reading ---------------------------------------------------------------------

def session_until(entry):
    """When a delegated entry lent for a SESSION ends (an aware datetime), else None."""
    raw = (entry.identity or {}).get("session_until") if entry.account == "delegated" else None
    if not raw:
        return None
    try:
        when = dt.datetime.fromisoformat(str(raw))
    except ValueError:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)  # unreadable: treat as ended
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def session_ended(entry, now=None) -> bool:
    until = session_until(entry)
    return until is not None and until <= (now or timezone.now())


def drop_ended_sessions(entries) -> list:
    """`entries` without the sessions that have ended, which are deleted: a session
    leaves nothing behind once it is over."""
    keep, ended = [], []
    for e in entries:
        (ended if session_ended(e) else keep).append(e)
    if ended:
        type(ended[0]).objects.filter(pk__in=[e.pk for e in ended]).delete()
    return keep


def serves(entry, agent) -> bool:
    """May a resolve hand `entry` out now. Never on a date — except a SESSION, which
    ends when its person said (and at Google, which issued no refresh token). A delegated entry stops the moment its person is no longer an admin of
    the agent (the owner counts; `is_admin` fails closed on someone who left the
    workspace). One delegated by another agent stops when that agent's owner changes."""
    if not is_delegated(entry, agent):
        return True
    if session_ended(entry):
        return False
    person = entry.delegated_by
    if person is None or not agent.is_admin(person):
        return False
    if entry.delegated_by_agent_id and entry.delegated_by_agent.owner_id != person.pk:
        return False
    return True


def machines_that_can_open(entry) -> list[str]:
    """Names of the machines a delegated value is sealed to, as stored."""

    jkts = {t for kind, t in zk_seal.sealed_targets(entry.value_zk) if kind == "device"}
    return sorted(set(RunnerDeviceKey.objects.filter(ecdh_jkt__in=jkts, status=RunnerDeviceKey.ACTIVE)
                      .values_list("runner__name", flat=True)))


def waiting_machines(entry, agent) -> list:
    """Machines that run `agent` and could open `entry`, but it was not sealed to them
    yet: a machine that holds it re-seals it to them (`reseal`)."""
    have = {t for kind, t in zk_seal.sealed_targets(entry.value_zk) if kind == "device"}
    return [k for k in entry_devices(entry, agent) if k.ecdh_jkt not in have]


def resolve_payload(agent, device_key) -> dict:
    """The `delegated` field of a runner's resolve, for the machine `device_key` proved:
    every delegated entry in force that was sealed to it, with what it needs beside the
    ciphertext (whose, which provider, the git identity) and the machines still waiting,
    so it can re-seal for them. Empty for any machine that does not run the agent."""
    if device_key is None or not device_key.ecdh_jkt:
        return {}
    out = {}
    for entry in delegated_entries(agent):
        if not serves(entry, agent):
            continue
        if ("device", device_key.ecdh_jkt) not in zk_seal.sealed_targets(entry.value_zk):
            continue
        # Sealed to it once is not enough: it must still be a machine this entry's person
        # lets hold it (a laptop of someone else's never is, whatever it was sealed to).
        if device_key.pk not in {k.pk for k in entry_devices(entry, agent)}:
            continue
        out[entry.name] = {
            "value_zk": entry.value_zk,
            "provider": entry.provider,
            "identity": entry.identity or {},
            "delegated_by_email": entry.delegated_by.email if entry.delegated_by_id else "",
            "waiting": [{"kind": "device", "target": k.ecdh_jkt, "public_jwk": k.ecdh_public_jwk,
                         "runner_name": k.runner.name} for k in waiting_machines(entry, agent)],
        }
    return out


def withheld_payload(agent) -> list[dict]:
    """What a read that gets no delegated credential is told about them: each one in force,
    whose it is and which provider — never a value — so someone using the agent on their
    own machine knows to use their own account for it (`canopy agent use` says so)."""
    return [{"name": e.name, "provider": e.provider, "label": label(e, agent),
             "delegated_by_email": e.delegated_by.email if e.delegated_by_id else "",
             "use": f"{e.provider or e.name}: {authority.stance(authority.DIRECT, authority.DELEGATED).why}"}
            for e in delegated_entries(agent) if serves(e, agent)]


def label(entry, agent) -> str:
    """"GitHub · delegated by Jonathan Jackson" — or "the agent's own" — plus what the
    provider said about expiry, or that a machine found it no longer works."""
    if not is_delegated(entry, agent):
        if entry.account == VaultEntryAccount.DELEGATED and entry.delegated_by_agent_id:
            return f"{PROVIDER_NAMES.get(entry.provider) or entry.provider} · {agent.slug}'s own account"
        return f"{agent.slug}'s own"
    provider = PROVIDER_NAMES.get(entry.provider) or entry.provider or "Credential"
    who = (entry.delegated_by_agent.slug if entry.delegated_by_agent_id else
           (entry.delegated_by.get_full_name() or entry.delegated_by.email) if entry.delegated_by_id else "?")
    out = f"{provider} · delegated by {who}"
    if any(not c.get("ok", True) for c in entry.checks or []):
        return out + " · no longer works — replace it"
    if entry.provider_expires_at:
        verb = "expired" if entry.provider_expires_at <= timezone.now() else "expires"
        out += f" · {provider} says it {verb} {entry.provider_expires_at:%-d %b %Y}"
    return out


def describe(entry, agent) -> dict:
    """Never a secret: what a screen shows of one delegated entry."""
    return {
        "name": entry.name,
        "account": entry.account,
        "label": label(entry, agent),
        "provider": entry.provider,
        "identity": entry.identity or {},
        "delegated_by_email": entry.delegated_by.email if entry.delegated_by_id else "",
        "delegated_by_agent": entry.delegated_by_agent.slug if entry.delegated_by_agent_id else "",
        "purpose": entry.purpose,
        "provider_expires_at": entry.provider_expires_at,
        "checks": entry.checks or [],
        "checked_at": entry.checked_at,
        "checked_by": entry.checked_by,
        "machines": machines_that_can_open(entry),
        "waiting": sorted(k.runner.name for k in waiting_machines(entry, agent)),
        "in_force": serves(entry, agent),
        "updated_at": entry.updated_at,
    }


# ---- a machine that holds it, re-sealing for a new one -------------------------------

def _recipient_rows(entry, agent) -> list[dict]:
    """Everyone `entry` may be sealed to now, with their public keys: the machines that
    run `agent`, and its person when they have a vault key."""

    out = [{"kind": "device", "target": k.ecdh_jkt, "public_jwk": k.ecdh_public_jwk,
            "runner_name": k.runner.name} for k in entry_devices(entry, agent)]
    ks = UserVaultKeyset.objects.filter(user_id=entry.delegated_by_id).first() if entry.delegated_by_id else None
    if ks is not None:
        out.append({"kind": "member", "target": str(ks.user_id), "public_jwk": ks.public_jwk,
                    "runner_name": ""})
    return out


def keeper_work(runner) -> list[dict]:
    """For each delegated entry this box holds (sealed to one of its keys, in force, for an
    agent it runs) that a machine is still waiting for: what the box needs to re-seal it —
    the stored value, everyone it may be sealed to, which of them wait, and the
    endorsements it checks a waiting machine against. canopy-web only lists; the box
    decides whom to trust (`canopy vault zk keep`)."""

    out = []
    for agent in Agent.objects.filter(pk__in=AgentCredential.objects.filter(account="delegated")
                                      .values("agent_id")):
        for entry in delegated_entries(agent):
            mine = [k for k in entry_devices(entry, agent) if k.runner_id == runner.pk]
            if not mine:
                continue
            sealed = zk_seal.sealed_targets(entry.value_zk)
            holder = next((k for k in mine if ("device", k.ecdh_jkt) in sealed), None)
            waiting = [k.ecdh_jkt for k in waiting_machines(entry, agent)]
            if holder is None or not waiting or not serves(entry, agent):
                continue
            out.append({
                "agent": agent.slug, "agent_id": str(agent.pk), "name": entry.name,
                "device_jkt": holder.ecdh_jkt, "value_zk": entry.value_zk,
                "expect": vault_engine.value_digest(entry.value_zk),
                "recipients": _recipient_rows(entry, agent), "waiting": waiting,
                "endorsements": [{**e, "agent_id": str(agent.pk)} for e in zk_endorse.endorsements(agent)],
                "reseal_path": f"/api/harness/runners/{runner.pk}/vault-keeper/{agent.slug}/delegated/{entry.name}",
            })
    return out


def reseal(agent, name: str, *, value_zk: str, expect: str, runner) -> list[str]:
    """Replace a delegated entry's sealed value with one a machine that opened it made,
    sealed to more machines. Every recipient must still be eligible (a machine that runs
    the agent, or the person it came from); every one it was sealed to that still is must
    stay; the writer — this runner — must have held it and must still. `expect` is the
    digest of the value it opened, checked under the agent's lock, so two keepers racing
    make one write. Returns the machines it is now sealed to."""


    value_zk = zk_seal.check_sealed_value(value_zk, name)
    with transaction.atomic():
        zk._locked(agent)
        entry = delegated_entries(agent).filter(name=name).first()
        if entry is None or not serves(entry, agent):
            raise ZkError(404, "not_found", f"{agent.slug} has no delegated credential {name}")
        if vault_engine.value_digest(entry.value_zk) != (expect or "").strip().lower():
            raise ZkError(409, "zk_value_changed", f"{name} changed since you read it")
        eligible = entry_devices(entry, agent)
        allowed = {("device", k.ecdh_jkt) for k in eligible}
        if entry.delegated_by_id:
            allowed.add(("member", str(entry.delegated_by_id)))
        old, given = zk_seal.sealed_targets(entry.value_zk), zk_seal.sealed_targets(value_zk)
        mine = {("device", k.ecdh_jkt) for k in eligible if k.runner_id == runner.pk}
        if not (mine & old) or not (mine & given):
            raise ZkError(403, "zk_not_a_holder", f"this runner holds no copy of {agent.slug}'s {name}")
        stray = sorted(t for _, t in given - allowed)
        if stray:
            raise ZkError(422, "zk_invalid_target",
                          f"{name} may be sealed only to the machines that run {agent.slug}: {', '.join(stray)}")
        if not (old & allowed) <= given:
            raise ZkError(422, "zk_rewrap_required", f"{name} must stay sealed to every machine that holds it")
        entry.value_zk = value_zk
        entry.save(update_fields=["value_zk", "updated_at"])
    return machines_that_can_open(entry)


# ---- dropping machines that may no longer hold it ------------------------------------

def prune(entry, agent) -> list[str]:
    """Drop from `entry`'s sealed value every recipient that may no longer hold it: a
    machine that is not one of `entry_devices` (retired, its owner no longer an admin,
    or a laptop of someone other than its person) and any person but its own. Each
    recipient has its own ciphertext, so this edits the envelope and decrypts nothing.
    A machine that opened it before keeps what it saw: its person replaces the token at
    the provider for that. Returns the runner names dropped; empty when nothing changed."""

    allowed = {("device", k.ecdh_jkt) for k in entry_devices(entry, agent)}
    if entry.delegated_by_id:
        allowed.add(("member", str(entry.delegated_by_id)))
    kept, dropped = zk_seal.keep_only(entry.value_zk, allowed)
    if not dropped:
        return []
    with transaction.atomic():
        zk._locked(agent)
        fresh = type(entry).objects.select_for_update().get(pk=entry.pk)
        if fresh.value_zk != entry.value_zk:
            return prune(fresh, agent)
        # Even when that leaves no machine: the panel then says nothing can open it, which
        # is true, and is better than a copy only a machine that may not hold it opens.
        fresh.value_zk = kept
        fresh.save(update_fields=["value_zk", "updated_at"])
    jkts = [t for kind, t in dropped if kind == "device"]
    names = dict(RunnerDeviceKey.objects.filter(ecdh_jkt__in=jkts).values_list("ecdh_jkt", "runner__name"))
    return sorted({names.get(t, t) for t in jkts} | {f"person {t}" for kind, t in dropped if kind == "member"})
