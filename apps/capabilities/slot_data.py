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

A row from before this (`secret_enc`, Fernet under the app key) is sealed by
`manage.py seal_slot_secrets`; until then its secret half is neither delivered
nor writable (409), and the listing marks it `secret_awaiting_seal`.
"""
from __future__ import annotations

import re

from django.db import transaction

from apps.agents.credentials import zk_seal
from apps.agents.credentials.zk import ZkError

from . import services
from .models import AgentCapability, CapabilityLease, CapabilitySlotData

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
        "secret_awaiting_seal": bool(row is not None and row.secret_enc),
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
        if secret and row is not None and row.secret_enc:
            raise services.CapabilityError(
                409, f"{slot}'s secret data is from before sealing: run `manage.py seal_slot_secrets` first")
        before = state(row)
        new_public = _merge(dict(row.public or {}) if row else {}, public)
        sealed = {k: (v if v is None else _seal(cap, slot, k, v)) for k, v in (secret or {}).items()}
        new_secret = _merge(_sealed_of(row), sealed)
        both = sorted(set(new_public) & set(new_secret))
        if both:
            raise services.CapabilityError(
                422, f"{', '.join(both)}: a key is either public or secret, not both "
                     "(send null in the other section to move it)")
        if not new_public and not new_secret and not (row is not None and row.secret_enc):
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
