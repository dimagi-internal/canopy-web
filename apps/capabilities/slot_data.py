"""Per-slot data of a `pool` capability: what each slot IS (docs/architecture/capabilities.md § 6).

A pool slot is a fixed thing — ACE's `worker-1` is one Connect test worker — and
whoever checks it out needs its details: a phone and a name (`public`), a PIN and
a backup code (`secret`). They live here, beside the lease, one row per slot.

Security rules this module keeps (and the tests pin):

- **Zero-knowledge.** A secret value is stored only SEALED (`zks1`, zk-v1 §6) to
  the principals that may read the agent — each eligible device's ECDH key and
  each vault member's vault key (`zk_seal.recipients_for`). canopy-web keeps no
  key that opens it. A writer seals before sending; a plaintext value (an MCP or
  script write) is sealed here at once and never stored or logged in clear. Its
  sealed name is `sealed_name(capability, slot, key)`.
- **Key names in clear.** The masked listing names the keys from the stored map;
  nothing is decrypted to list them (nothing here can decrypt).
- **Delivered to exactly one caller:** the user who acquired a pool lease
  (`requested_by`), while that lease is `ready`, for the slot it holds, and only
  while the manifest still declares that slot — still sealed; the holder opens
  it with its device key (`canopy vault open-slot-data`). Every delivery records
  `capability.slot.secret_delivered` (lease, slot, user, key NAMES).
- **Never in a log.** Events, errors, `__str__` and the settings audit carry key
  names at most; a refusal names the key, never the value.

**Follows the agent's readers.** A seal reaches the readers of its moment. When
the agent gains one (a machine approved, a vault member with a vault key), a box
that holds the value re-seals it for them — the keeper (`keeper_work` /
`reseal`, `canopy vault zk keep`): it opens the value with its own device key,
checks the newcomer's endorsement, and sends back a new `zks1`; canopy-web only
lists who waits and checks who the new value is sealed to. When the agent loses
one, `prune` drops it from every value's envelope (a tick, `scheduler.py`),
decrypting nothing.
"""
from __future__ import annotations

import logging
import re

from django.db import transaction

from apps.agents.credentials import vault_engine, zk, zk_endorse, zk_seal
from apps.agents.credentials.zk import ZkError

from . import services
from .models import AgentCapability, CapabilityLease, CapabilitySlotData

logger = logging.getLogger(__name__)

SECTIONS = ("public", "secret")
KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_KEYS = 50
MAX_VALUE = 4096


def sealed_name(capability: str, slot: str, key: str) -> str:
    """The name a slot secret is sealed under (bound into its AAD and HKDF info).
    The `canopy` CLI builds the same string to open it."""
    return f"capability-slot:{capability}/{slot}/{key}"


def _sealed_of(row: CapabilitySlotData | None) -> dict[str, str]:
    return dict(row.secret_zk or {}) if row is not None else {}


def _seal(cap: AgentCapability, slot: str, key: str, value: str) -> str:
    """A value as stored: an already-sealed `zks1` checked for shape, else
    plaintext sealed now to everyone who may read the agent."""
    if value.startswith(zk_seal.SEALED_PREFIX):
        try:
            return zk_seal.check_sealed_value(value, f"secret.{key}")
        except ZkError as e:
            raise services.CapabilityError(422, str(e)) from None
    agent = cap.agent
    try:
        return zk_seal.seal_to(value, agent_id=str(agent.pk), name=sealed_name(cap.name, slot, key),
                               kv=agent.zk_kv, recipients=zk_seal.recipients_for(agent))
    except ZkError as e:
        raise services.CapabilityError(e.status, f"{key}: {e}") from None


def declared_slots(cap: AgentCapability) -> list[str]:
    return list(services.manifest_of(cap).slots or [])


def _require_pool(cap: AgentCapability) -> None:
    if not services.is_pool(cap):
        raise services.CapabilityError(
            422, f"{cap.name} is a {cap.kind} capability: only a pool capability has slots to hold data")


def _require_declared(cap: AgentCapability, slot: str) -> None:
    _require_pool(cap)
    if cap.retired_at is not None:
        raise services.CapabilityError(422, f"{cap.name} is retired: publish it again to set its slots' data")
    declared = declared_slots(cap)
    if slot not in declared:
        raise services.CapabilityError(
            422, f"{slot!r} is not one of {cap.name}'s slots ({', '.join(declared)})")


def _validate(section: str, changes: dict) -> None:
    if len(changes) > MAX_KEYS:
        raise services.CapabilityError(422, f"{section}: at most {MAX_KEYS} keys")
    for key, value in changes.items():
        if not KEY_RE.match(str(key)):
            raise services.CapabilityError(
                422, f"{section}: key {str(key)[:64]!r} must match [A-Za-z0-9_.-]{{1,64}}")
        if value is None:
            continue
        # The refusal names the KEY, never the value: this may be the secret half.
        if not isinstance(value, str):
            raise services.CapabilityError(422, f"{section}.{key}: a value is a string (or null to delete)")
        if not value.strip():
            raise services.CapabilityError(422, f"{section}.{key}: blank value — send null to delete a key")
        limit = (zk_seal.MAX_VALUE_ZK_LEN if section == "secret" and value.startswith(zk_seal.SEALED_PREFIX)
                 else MAX_VALUE)
        if len(value) > limit:
            raise services.CapabilityError(422, f"{section}.{key}: longer than {limit} characters")


def _merge(current: dict[str, str], changes: dict | None) -> dict[str, str]:
    out = dict(current)
    for key, value in (changes or {}).items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def held_by(cap: AgentCapability) -> dict[str, CapabilityLease]:
    """The live lease holding each slot."""
    return {lease.slot: lease for lease in
            cap.leases.filter(status__in=CapabilityLease.LIVE).exclude(slot="")}


def masked(cap: AgentCapability, slot: str, row: CapabilitySlotData | None,
           holder: CapabilityLease | None) -> dict:
    """What anyone may read about a slot's data: public values, secret key NAMES."""
    return {
        "slot": slot,
        "declared": slot in declared_slots(cap),
        "public": dict(row.public or {}) if row else {},
        "secret_keys": sorted(_sealed_of(row)),
        "updated_at": row.updated_at if row else None,
        "updated_by": (getattr(row.updated_by, "email", None) or None) if row else None,
        "held": holder is not None,
        "lease_id": str(holder.pk) if holder is not None else None,
    }


def state(row: CapabilitySlotData | None) -> dict | None:
    """The masked state for the settings audit's before/after: never a secret value."""
    if row is None:
        return None
    return {"public": dict(row.public or {}), "secret_keys": sorted(_sealed_of(row))}


def listing(cap: AgentCapability) -> list[dict]:
    """Every declared slot, in declared order, masked."""
    _require_pool(cap)
    rows = {r.slot: r for r in CapabilitySlotData.objects.filter(capability=cap).select_related("updated_by")}
    holders = held_by(cap)
    return [masked(cap, s, rows.get(s), holders.get(s)) for s in declared_slots(cap)]


def put(cap: AgentCapability, slot: str, *, public: dict | None, secret: dict | None, by
        ) -> tuple[CapabilitySlotData | None, dict | None]:
    """Per-key upsert: a key set to null is deleted, a key left out is kept, a
    section left out (None) is untouched. A slot left with no keys at all has no
    row. Returns (the row or None, the masked state before)."""
    _require_declared(cap, slot)
    for section, changes in (("public", public), ("secret", secret)):
        if changes is not None:
            _validate(section, changes)
    with transaction.atomic():
        row = CapabilitySlotData.objects.select_for_update().filter(capability=cap, slot=slot).first()
        before = state(row)
        new_public = _merge(dict(row.public or {}) if row else {}, public)
        sealed = {k: (v if v is None else _seal(cap, slot, k, v)) for k, v in (secret or {}).items()}
        new_secret = _merge(_sealed_of(row), sealed)
        both = sorted(set(new_public) & set(new_secret))
        if both:
            raise services.CapabilityError(
                422, f"{', '.join(both)}: a key is either public or secret, not both "
                     "(send null in the other section to move it)")
        if not new_public and not new_secret:
            if row is not None:
                row.delete()
            row = None
        else:
            if row is None:
                row = CapabilitySlotData(capability=cap, slot=slot)
            row.public = new_public
            row.secret_zk = new_secret
            row.updated_by = by if getattr(by, "is_authenticated", False) else None
            row.save()
    services._cap_event(cap, "slot.data_set",
                        f"{cap.agent.slug}/{cap.name} slot {slot} data set",
                        slot=slot, public_keys=sorted(new_public), secret_keys=sorted(new_secret),
                        by=getattr(by, "email", "") or "")
    return row, before


def delete(cap: AgentCapability, slot: str, *, by) -> dict | None:
    """Removes a slot's data — also a slot the manifest no longer declares (its
    data is kept but never delivered; this is how it goes). Idempotent. Returns
    the masked state before."""
    _require_pool(cap)
    row = CapabilitySlotData.objects.filter(capability=cap, slot=slot).first()
    before = state(row)
    if row is not None:
        row.delete()
        services._cap_event(cap, "slot.data_deleted", f"{cap.agent.slug}/{cap.name} slot {slot} data deleted",
                            slot=slot, by=getattr(by, "email", "") or "")
    return before


def deliver(lease: CapabilityLease, user) -> dict | None:
    """The slot's data for the lease's OWN requester while it is ready — else
    None. The secret half goes out still sealed (`sealed`), with the agent id
    it is bound to; canopy-web cannot open it. Records each delivery."""
    if (lease.status != CapabilityLease.READY or not lease.slot or not services.is_pool(lease.capability)
            or lease.requested_by_id is None or lease.requested_by_id != getattr(user, "pk", None)):
        return None
    if lease.slot not in declared_slots(lease.capability):
        return None  # kept, never delivered
    row = CapabilitySlotData.objects.filter(capability=lease.capability, slot=lease.slot).first()
    if row is None:
        return None
    sealed = _sealed_of(row)
    if sealed:
        services._event(lease, "slot.secret_delivered",
                        f"{lease.agent.slug}/{lease.capability.name} slot {lease.slot}: sealed secret data "
                        f"delivered to {getattr(user, 'email', '') or user.pk}",
                        slot=lease.slot, user=getattr(user, "email", "") or str(user.pk),
                        secret_keys=sorted(sealed))
    return {"public": dict(row.public or {}), "sealed": sealed, "agent_id": str(lease.agent_id)}


# ---- following the agent's readers -----------------------------------------------------

def _recipient_rows(agent) -> list[dict]:
    """Everyone a slot secret of `agent` may be sealed to now, with their public keys:
    each eligible machine and each vault member with a vault key (`recipients_for`)."""
    out = [{"kind": "device", "target": k.ecdh_jkt, "public_jwk": k.ecdh_public_jwk,
            "runner_name": k.runner.name} for k in zk.eligible_device_keys(agent)]
    out += [{"kind": "member", "target": str(ks.user_id), "public_jwk": ks.public_jwk, "runner_name": ""}
            for ks in zk.eligible_members(agent)]
    return out


def _sealed_rows():
    return (CapabilitySlotData.objects.exclude(secret_zk={})
            .select_related("capability__agent").order_by("capability_id", "slot"))


def _held_by(runner, agent) -> set[str]:
    """The ECDH thumbprints of this runner's machines among `agent`'s readers."""
    return {k.ecdh_jkt for k in zk.eligible_device_keys(agent) if k.runner_id == runner.pk}


def keeper_work(runner) -> list[dict]:
    """For each slot secret this box holds (sealed to one of its eligible machines) that
    a reader of the agent is still waiting for: what the box needs to re-seal it — the
    stored value, everyone it may be sealed to, which of them wait, and the endorsements
    it checks a waiting machine against. canopy-web only lists; the box decides whom to
    trust (`canopy vault zk keep`). The same shape as a delegated credential's work."""
    out: list[dict] = []
    seen: dict = {}
    for row in _sealed_rows():
        cap, agent = row.capability, row.capability.agent
        if agent.pk not in seen:
            seen[agent.pk] = {"mine": _held_by(runner, agent), "rows": None, "ends": None}
        facts = seen[agent.pk]
        if not facts["mine"]:
            continue
        if facts["rows"] is None:
            facts["rows"] = _recipient_rows(agent)
        for key, value in sorted((row.secret_zk or {}).items()):
            sealed = zk_seal.sealed_targets(value)
            holder = next((t for t in sorted(facts["mine"]) if ("device", t) in sealed), None)
            waiting = [r["target"] for r in facts["rows"] if (r["kind"], r["target"]) not in sealed]
            if holder is None or not waiting:
                continue
            if facts["ends"] is None:
                facts["ends"] = [{**e, "agent_id": str(agent.pk)} for e in zk_endorse.endorsements(agent)]
            out.append({
                "agent": agent.slug, "agent_id": str(agent.pk), "name": sealed_name(cap.name, row.slot, key),
                "device_jkt": holder, "value_zk": value, "expect": vault_engine.value_digest(value),
                "recipients": facts["rows"], "waiting": waiting, "endorsements": facts["ends"],
                "reseal_path": (f"/api/harness/runners/{runner.pk}/vault-keeper/{agent.slug}/slot-secrets/"
                                f"{cap.name}/{row.slot}/{key}"),
            })
    return out


def reseal(agent, capability: str, slot: str, key: str, *, value_zk: str, expect: str, runner) -> list[str]:
    """Replace a slot secret with one a machine that opened it made, sealed to more of
    the agent's readers. Every recipient must be one now (`recipients_for`); every one it
    was sealed to that still is must stay; the writer — this runner — must have held it
    and must still. `expect` is the digest of the value it opened, checked under the
    row's lock, so two keepers racing make one write. Returns the machines (runner
    names) it is now sealed to."""
    name = sealed_name(capability, slot, key)
    value_zk = zk_seal.check_sealed_value(value_zk, name)
    with transaction.atomic():
        row = (CapabilitySlotData.objects.select_for_update()
               .filter(capability__agent=agent, capability__name=capability, slot=slot).first())
        current = (row.secret_zk or {}).get(key) if row is not None else None
        if not current:
            raise ZkError(404, "not_found", f"{agent.slug}/{capability} slot {slot} has no secret {key}")
        if vault_engine.value_digest(current) != (expect or "").strip().lower():
            raise ZkError(409, "zk_value_changed", f"{key} changed since you read it")
        rows = _recipient_rows(agent)
        allowed = {(r["kind"], r["target"]) for r in rows}
        old, given = zk_seal.sealed_targets(current), zk_seal.sealed_targets(value_zk)
        mine = {("device", t) for t in _held_by(runner, agent)}
        if not (mine & old) or not (mine & given):
            raise ZkError(403, "zk_not_a_holder", f"this runner holds no copy of {name}")
        stray = sorted(t for _, t in given - allowed)
        if stray:
            raise ZkError(422, "zk_invalid_target",
                          f"{key} may be sealed only to {agent.slug}'s readers: {', '.join(stray)}")
        if not (old & allowed) <= given:
            raise ZkError(422, "zk_rewrap_required", f"{key} must stay sealed to every reader that holds it")
        row.secret_zk = {**row.secret_zk, key: value_zk}
        row.save(update_fields=["secret_zk", "updated_at"])
    services._cap_event(row.capability, "slot.secret_resealed",
                        f"{agent.slug}/{capability} slot {slot}: {key} re-sealed by {runner.name}",
                        slot=slot, secret_keys=[key], runner=runner.name)
    names = {r["target"]: r["runner_name"] for r in rows if r["kind"] == "device"}
    return sorted({names[t] for k, t in given if k == "device" and t in names})


def prune(agent=None) -> dict[str, list[str]]:
    """Drop, from every slot secret (of `agent`, or of every agent), each recipient that
    is no longer one of its agent's readers: a machine revoked, retired, no longer
    serving it or whose person left the vault; a person who left it. Each recipient has
    its own ciphertext, so this edits the envelope and decrypts nothing — even when that
    leaves nobody, and the value must be written again. Returns
    {"<agent>/<capability>/<slot>/<key>": [targets dropped]} for what changed."""
    rows = _sealed_rows()
    if agent is not None:
        rows = rows.filter(capability__agent=agent)
    allowed_of: dict = {}
    changed: dict[str, list[str]] = {}
    for row in rows:
        a = row.capability.agent
        if a.pk not in allowed_of:
            allowed_of[a.pk] = {(r["kind"], r["target"]) for r in _recipient_rows(a)}
        allowed = allowed_of[a.pk]
        if not any(zk_seal.keep_only(v, allowed)[1] for v in (row.secret_zk or {}).values()):
            continue
        with transaction.atomic():
            fresh = CapabilitySlotData.objects.select_for_update().get(pk=row.pk)
            new = {}
            for key, value in (fresh.secret_zk or {}).items():
                kept, dropped = zk_seal.keep_only(value, allowed)
                new[key] = kept
                if dropped:
                    changed[f"{a.slug}/{row.capability.name}/{row.slot}/{key}"] = sorted(t for _, t in dropped)
            fresh.secret_zk = new
            fresh.save(update_fields=["secret_zk", "updated_at"])
    return changed


def prune_tick() -> None:
    """The scheduler's job: `prune` every agent, noting what it dropped."""
    for where, dropped in prune().items():
        logger.info("slot secrets: dropped %s from %s (no longer a reader)", ", ".join(dropped), where)
