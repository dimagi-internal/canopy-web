"""The one set of zero-knowledge vault rules, for an agent's vault and a workspace vault.

Both kinds of vault speak the same formats (`docs/vault/zk-v1/`) and obey the same
rules: who may be handed a copy of the key (a wrap), what a write that makes or
replaces the key must carry (a way back, a copy the writer can open, every value
re-encrypted), the key-version checks, and the read a member's client grants and
rotates from. They differ only in where their rows live — an agent's on the agent
(`AgentVaultWrap`, `AgentCredential`, `Agent.zk_kv`), a workspace vault's on the
vault (`VaultKeyWrap`, `VaultSecret`, `Vault.kv`) — and in who is eligible, which
each kind's module decides. A `Store` names those differences; everything else is
written here once, so a rule added to one kind of vault reaches the other.

`zk.py` (an agent's vault) and `workspace_vaults.py` keep their public functions and
build their store; nothing outside them calls this module.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from django.db import transaction

# A module reference, not names: `zk` imports this module while it is still loading.
from . import zk as _zk


@dataclass
class Store:
    """Where one vault's rows live, and who may hold its key."""

    #: How the vault is named in a message ("echo", "Acme shared").
    label: str
    #: Its current key version (0: no key yet).
    kv: int
    #: The wrap model, and the keyword it takes the vault by (`agent=` or `vault=`).
    wrap_model: Any
    secret_model: Any
    owner_kw: dict
    eligible_devices: Callable[[], list]
    eligible_members: Callable[[], list]
    #: Re-read the vault's row under a lock and return its store.
    lock: Callable[[], "Store"]
    #: Record the new key version.
    save_kv: Callable[[int], None]
    #: Refuse when this vault cannot take zero-knowledge writes at all.
    require_enabled: Callable[[], None] = lambda: None
    #: A `zks1` value (sealed to recipients before a key exists) is accepted.
    accepts_sealed: bool = False
    #: Message tails.
    device_scope: str = ""
    member_scope: str = ""
    extra: dict = field(default_factory=dict)

    def wraps(self):
        return self.wrap_model.objects.filter(**self.owner_kw)

    def secrets(self):
        return self.secret_model.objects.filter(**self.owner_kw)


def wrap_key(w) -> tuple:
    if w.kind == "device":
        return ("device", w.device_key_id)
    if w.kind == "member":
        return ("member", w.user_id)
    return ("recovery",)


def _refuse_logins_that_are_not_people(member_targets, what: str) -> None:
    """No copy of any vault's key, and no value sealed to one, is ever written for an
    agent's own login or a system account — an agent login holding a TEST vault key
    (`login_test_key`) included. `eligible_members` already leaves them out (they are
    never members); this refuses them by name, whatever eligibility says."""
    from .vault_members import not_a_person  # noqa: PLC0415 — vault_members imports zk

    ids = set()
    for t in member_targets:
        try:
            ids.add(int(str(t)))
        except ValueError:
            continue
    if ids and not_a_person(ids):
        raise _zk.ZkError(403, "not_a_person",
                          f"{what}: an agent's login or a system account is not a person, and holds "
                          "nothing in any vault")


def parse_wraps(store: Store, kv: int, wraps: list, *, allow_recovery: bool) -> list:
    """Validate `[{kind, target, wrap}]` and return unsaved wrap rows."""
    _refuse_logins_that_are_not_people(
        [item.target for item in wraps if item.kind == "member"], "wraps")
    devices = {str(k.pk): k for k in store.eligible_devices()}
    members = {str(k.user_id): k for k in store.eligible_members()}
    out, seen = [], set()
    for i, item in enumerate(wraps):
        what = f"wraps[{i}]"
        kind, target, wrap = item.kind, item.target, item.wrap
        if kind == "device":
            try:
                key_id = str(uuid.UUID(str(target)))
            except ValueError as exc:
                raise _zk._shape(f"{what}: a device target is a device key id") from exc
            key = devices.get(key_id)
            if key is None:
                raise _zk.ZkError(422, "zk_invalid_target",
                              f"{what}: device key {key_id} is not an active device key with an ECDH "
                              f"key {store.device_scope}")
            row = store.wrap_model(**store.owner_kw, kv=kv, kind=kind, device_key=key,
                                   wrap=_zk.check_ecdh_wrap(wrap, what))
        elif kind == "member":
            ks = members.get(str(target))
            if ks is None:
                raise _zk.ZkError(422, "zk_invalid_target",
                              f"{what}: user {target} is not a member of {store.member_scope} "
                              "with a vault key (Settings → Vault key)")
            row = store.wrap_model(**store.owner_kw, kv=kv, kind=kind, user=ks.user,
                                   wrap=_zk.check_ecdh_wrap(wrap, what))
        elif kind == "recovery":
            if not allow_recovery:
                raise _zk.ZkError(422, "zk_invalid_target",
                              f"{what}: the recovery wrap is set only by enable or rotate")
            if target not in (None, ""):
                raise _zk._shape(f"{what}: a recovery wrap has no target")
            row = store.wrap_model(**store.owner_kw, kv=kv, kind=kind, wrap=_zk.check_recovery_wrap(wrap, what))
        else:
            raise _zk._shape(f"{what}: kind must be device, member or recovery")
        k = wrap_key(row)
        if k in seen:
            raise _zk._shape(f"{what}: a second wrap for the same target")
        seen.add(k)
        out.append(row)
    return out


def caller_can_open(rows: list, user) -> bool:
    """A member wrap to the caller's own keyset, or a device wrap to a key on a
    runner the caller OWNS (the canopy CLI acting as its device)."""
    return any((w.kind == "member" and w.user_id == user.pk)
               or (w.kind == "device" and w.device_key.runner.owner_id == user.pk) for w in rows)


def check_full_set(rows: list, user) -> None:
    """What a write that makes or replaces the key must carry: a way back if every
    passkey is lost (one recovery wrap, or a member wrap to someone whose own vault key
    has a recovery code), and a copy the writer can open."""
    recovery = [w for w in rows if w.kind == "recovery"]
    if len(recovery) > 1:
        raise _zk.ZkError(422, "zk_recovery_required", "at most one recovery wrap")
    if not recovery and not any(w.kind == "member" and _zk.has_recovery_seal(w.user_id) for w in rows):
        raise _zk.ZkError(422, "zk_recovery_required",
                      "include a recovery wrap, or a member wrap to someone whose vault key "
                      "has a recovery code")
    if not caller_can_open(rows, user):
        raise _zk.ZkError(422, "zk_caller_wrap_required",
                      "include a wrap you can open: a member wrap to your own vault key, or a device "
                      "wrap to a device key on a runner you own — else nobody you trust holds "
                      "the new vault key")


def parse_values(store: Store, values: list, *, allow_sealed: bool = False) -> dict[str, str]:
    """`allow_sealed`: a `zks1:` value is accepted too — only where the store takes
    them, and never by enable/rotate, which must leave every value `zk1`."""
    from .zk_seal import (  # cycle: agents.zk_seal -> agents.zk  # noqa: PLC0415
        SEALED_PREFIX, check_sealed_value, sealed_targets)

    sealed_ok = allow_sealed and store.accepts_sealed
    out: dict[str, str] = {}
    for i, v in enumerate(values):
        name = (v.name or "").strip()
        if not name:
            raise _zk._shape(f"values[{i}]: name is required")
        if name in out:
            raise _zk._shape(f"values[{i}]: {name} appears twice")
        if sealed_ok and isinstance(v.value_zk, str) and v.value_zk.startswith(SEALED_PREFIX):
            out[name] = check_sealed_value(v.value_zk, name)
            _refuse_logins_that_are_not_people(
                [t for kind, t in sealed_targets(out[name]) if kind == "member"], f"values[{i}]")
        else:
            out[name] = _zk.check_value_zk(v.value_zk, name)
    return out


def require_coverage(store: Store, names) -> None:
    """Every stored value must be in the body: one left behind would be under a
    retired key (rotate), or custodial plaintext (an agent's enable)."""
    missing = sorted(set(store.secrets().values_list("name", flat=True)) - set(names))
    if missing:
        raise _zk.ZkError(422, "zk_values_incomplete",
                      "every stored credential must be included; missing: " + ", ".join(missing))


def value_digest(value_zk: str) -> str:
    """What a compare-and-swap `expect` names: hex SHA-256 of the stored text."""
    return hashlib.sha256(value_zk.encode()).hexdigest() if value_zk else ""


def check_expected(store: Store, values: list) -> None:
    """Compare-and-swap for a values write: every value carrying `expect` must still
    be what the caller read, under the vault's row lock."""
    want = {(v.name or "").strip(): v.expect for v in values if getattr(v, "expect", None) is not None}
    if not want:
        return
    have = dict(store.secrets().filter(name__in=list(want)).values_list("name", "value_zk"))
    changed = sorted(n for n, e in want.items() if value_digest(have.get(n, "")) != e.strip().lower())
    if changed:
        raise _zk.ZkError(409, "zk_value_changed",
                      "changed since you read it (re-read and try again): " + ", ".join(changed))


def _by(user):
    return user if getattr(user, "is_authenticated", False) else None


def write_values(store: Store, values: dict[str, str], kv: int, user) -> None:
    defaults = {"kv": kv, "updated_by": _by(user), **store.extra.get("value_defaults", {})}
    for name, value_zk in values.items():
        store.secret_model.objects.update_or_create(**store.owner_kw, name=name,
                                                    defaults={**defaults, "value_zk": value_zk})


def save_wraps(rows: list, user, store: Store) -> None:
    for w in rows:
        w.created_by = _by(user)
    store.wrap_model.objects.bulk_create(rows)


def _require_kv(store: Store, kv: int) -> None:
    if kv != store.kv:
        raise _zk.ZkError(409, "zk_kv_mismatch", f"the vault key is at kv {store.kv}, not {kv}")


# ---- the writes ---------------------------------------------------------------------

def enable(store: Store, *, kv: int, wraps: list, values: list, user, already: str) -> Store:
    """Make the vault's first key (kv 1), atomically: its copies, every value as
    ciphertext. `already` is the refusal when it has a key. Returns the locked store,
    for the caller to finish its own flags inside the same transaction."""
    if kv != 1:
        raise _zk.ZkError(422, "zk_kv_mismatch", "enable starts the vault key at kv 1")
    store = store.lock()
    if store.kv != 0:
        raise _zk.ZkError(409, "zk_already_enabled", already)
    rows = parse_wraps(store, kv, wraps, allow_recovery=True)
    check_full_set(rows, user)
    vals = parse_values(store, values)
    require_coverage(store, vals)
    store.wraps().delete()
    save_wraps(rows, user, store)
    write_values(store, vals, kv, user)
    store.save_kv(kv)
    return store


def set_values(store: Store, *, kv: int, values: list, user) -> list[str]:
    """Upsert ciphertext values at the current kv (non-clobbering)."""
    with transaction.atomic():
        store = store.lock()
        store.require_enabled()
        if store.kv == 0 and not store.accepts_sealed:
            raise _zk.ZkError(409, "zk_no_vault_key", f"{store.label} has no vault key yet")
        _require_kv(store, kv)
        vals = parse_values(store, values, allow_sealed=True)
        if store.kv == 0 and any(not v.startswith("zks1:") for v in vals.values()):
            raise _zk.ZkError(409, "zk_no_vault_key",
                          f"{store.label} has no vault key yet: seal values to its recipients (zks1), "
                          "or create the vault key first")
        check_expected(store, values)
        write_values(store, vals, kv, user)
    return sorted(vals)


def add_wraps(store: Store, *, kv: int, wraps: list, user, only_new: bool = False) -> int:
    """Grant: add (or replace) device/member wraps at the current kv. `only_new` (a
    keeper) refuses a target that already holds a copy, checked under the vault's row
    lock so two keepers racing make exactly one grant."""
    with transaction.atomic():
        store = store.lock()
        store.require_enabled()
        _require_kv(store, kv)
        if kv == 0:
            raise _zk.ZkError(409, "zk_no_vault_key", f"{store.label} has no vault key yet to grant")
        rows = parse_wraps(store, kv, wraps, allow_recovery=False)
        for w in rows:
            held = store.wraps().filter(kv=kv, kind=w.kind, device_key=w.device_key, user=w.user)
            if only_new and held.exists():
                raise _zk.ZkError(409, "zk_not_pending",
                              f"{w.kind} {w.device_key_id or w.user_id} already holds a copy — a keeper "
                              "only adds copies, never replaces one")
            held.delete()
        save_wraps(rows, user, store)
    return len(rows)


def rotate(store: Store, *, new_kv: int, wraps: list, values: list, user) -> None:
    """A fresh vault key: replace every wrap and every value atomically. Every target
    that holds a wrap now and still qualifies must get one at new_kv — so a person or
    machine removed since is left out, and the copy they kept opens nothing new."""
    with transaction.atomic():
        store = store.lock()
        store.require_enabled()
        if store.kv == 0:
            raise _zk.ZkError(409, "zk_no_vault_key", f"{store.label} has no vault key yet: create it (enable)")
        if new_kv != store.kv + 1:
            raise _zk.ZkError(409, "zk_kv_mismatch",
                          f"the vault key is at kv {store.kv}; rotate to kv {store.kv + 1}")
        rows = parse_wraps(store, new_kv, wraps, allow_recovery=True)
        check_full_set(rows, user)
        given = {wrap_key(w) for w in rows}
        eligible = ({("device", k.pk) for k in store.eligible_devices()}
                    | {("member", k.user_id) for k in store.eligible_members()})
        current = {wrap_key(w) for w in store.wraps().filter(kv=store.kv) if w.kind != "recovery"}
        missing = sorted(str(k[1]) for k in (current & eligible) - given)
        if missing:
            raise _zk.ZkError(422, "zk_rewrap_required",
                          "re-wrap for every remaining target; missing: " + ", ".join(missing))
        vals = parse_values(store, values)
        require_coverage(store, vals)
        store.wraps().delete()
        save_wraps(rows, user, store)
        write_values(store, vals, new_kv, user)
        store.save_kv(new_kv)


# ---- the read a member's client grants and rotates from ----------------------------------

def device_target(k) -> dict:
    return {"kind": "device", "target": str(k.pk), "runner": str(k.runner_id),
            "runner_name": k.runner.name, "label": k.label, "device_kind": k.kind,
            "ecdh_jkt": k.ecdh_jkt, "public_jwk": k.ecdh_public_jwk, "ecdh_public_jwk": k.ecdh_public_jwk}


def member_target(ks) -> dict:
    return {"kind": "member", "target": str(ks.user_id), "email": ks.user.email,
            "public_jwk": ks.public_jwk}


def pending_targets(store: Store, *, has_key: bool) -> list[dict]:
    """Eligible principals with no wrap at the current kv: who should be granted to
    next. Before the key exists, every eligible principal (what enable wraps to)."""
    have = {wrap_key(w) for w in store.wraps().filter(kv=store.kv)} if has_key else set()
    out = [device_target(k) for k in store.eligible_devices() if ("device", k.pk) not in have]
    out += [member_target(ks) for ks in store.eligible_members() if ("member", ks.user_id) not in have]
    return out


def wrap_rows(store: Store) -> list[dict]:
    """Every wrap, public data, each saying whether its holder still qualifies."""
    devices = {k.pk for k in store.eligible_devices()}
    members = {ks.user_id: ks for ks in store.eligible_members()}
    out = []
    for w in (store.wraps().select_related("device_key__runner", "user", "created_by")
              .order_by("kv", "kind", "id")):
        row = {"kind": w.kind, "kv": w.kv, "wrap": w.wrap, "created_at": w.created_at,
               "created_by_email": w.created_by.email if w.created_by_id else "",
               "target": None, "label": "", "current": True}
        if w.kind == "device":
            k = w.device_key
            row.update(target=str(k.pk), label=f"{k.runner.name} · {k.label or k.kind}",
                       ecdh_jkt=k.ecdh_jkt, current=k.pk in devices, public_jwk=k.ecdh_public_jwk or None,
                       ecdh_public_jwk=k.ecdh_public_jwk or None)
        elif w.kind == "member":
            ks = members.get(w.user_id)
            row.update(target=str(w.user_id), label=w.user.email, current=ks is not None,
                       public_jwk=ks.public_jwk if ks is not None else None)
        out.append(row)
    return out


def rotation_recommended(wraps: list[dict]) -> bool:
    """A wrap whose holder no longer qualifies (key revoked, member removed) still opens
    the key it was made for: only a rotation retires it."""
    return any(not w["current"] for w in wraps if w["kind"] != "recovery")
