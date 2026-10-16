"""Zero-knowledge vault for an agent (Canopy Vault Phase 2, canopy-web#1550).

For an agent with `zk_enabled`, canopy-web stores only ciphertext and wrapped
keys. Every byte format is defined by `docs/vault/zk-v1/zk_v1_reference.py`
(with test vectors beside it); this module never encrypts, decrypts or unwraps
anything. It checks SHAPES — the algorithm tag, base64url lengths, the `zk1:`
prefix — and enforces WHO may hold a wrap:

* a `device` wrap goes to an ACTIVE RunnerDeviceKey that has an ECDH key, on a
  runner this agent's resolve accepts proofs from (`runner_serves_agent`);
* a `member` wrap goes to a current agent admin (`Agent.is_admin`) who has a
  UserVaultKeyset;
* exactly one `recovery` wrap per vault-key version, set at enable/rotate.

A write that leaves the caller unable to open the result is refused: enable
and rotate need a wrap the CALLER can open — a member wrap to their own keyset,
or a device wrap to a device key on a runner they own or administer (the
`canopy` CLI acts as its device).
"""
from __future__ import annotations

import base64
import binascii
import re
import uuid
from dataclasses import dataclass

from django.db import transaction

ZK_PREFIX = "zk1:"
NONCE_LEN, TAG_LEN, KEY_LEN, SALT_LEN, POINT_LEN = 12, 16, 32, 16, 65
#: A wrapped 32-byte key (VK, or a person's private scalar) plus its GCM tag.
WRAPPED_LEN = KEY_LEN + TAG_LEN
#: Generous: a value is a secret, not a file.
MAX_VALUE_ZK_LEN = 64 * 1024
_B64U = re.compile(r"^[A-Za-z0-9_-]*$")


class ZkError(Exception):
    """A refused zero-knowledge write. `code` is the machine-readable `error`."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def _shape(message: str) -> ZkError:
    return ZkError(422, "zk_invalid_shape", message)


# ---- shape checks (never decryption) ------------------------------------------

def b64u_bytes(value, what: str) -> bytes:
    """Decode unpadded base64url, refusing padding or any other alphabet."""
    if not isinstance(value, str) or not _B64U.match(value) or len(value) % 4 == 1:
        raise _shape(f"{what} must be base64url without padding")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise _shape(f"{what} must be base64url without padding") from exc


def _exact(value, n: int, what: str) -> None:
    if len(b64u_bytes(value, what)) != n:
        raise _shape(f"{what} must decode to {n} bytes")


def _keys(obj, expected: set[str], what: str) -> dict:
    if not isinstance(obj, dict) or set(obj) != expected:
        raise _shape(f"{what} must be an object with exactly {sorted(expected)}")
    return obj


def check_value_zk(value, name: str = "value") -> str:
    """`zk1:` + b64u(nonce12 || ciphertext || tag16)."""
    if not isinstance(value, str) or not value.startswith(ZK_PREFIX):
        raise _shape(f"{name}: value_zk must start with {ZK_PREFIX!r}")
    if len(value) > MAX_VALUE_ZK_LEN:
        raise _shape(f"{name}: value_zk is too long")
    if len(b64u_bytes(value[len(ZK_PREFIX):], f"{name}: value_zk")) < NONCE_LEN + TAG_LEN:
        raise _shape(f"{name}: value_zk is shorter than a nonce and a tag")
    return value


def check_ecdh_wrap(wrap, what: str = "wrap") -> dict:
    """{"alg": "ecdh-es", "epk": 65-byte uncompressed point, "n": 12, "ct": 48}."""
    _keys(wrap, {"alg", "epk", "n", "ct"}, what)
    if wrap["alg"] != "ecdh-es":
        raise _shape(f"{what}: alg must be 'ecdh-es'")
    epk = b64u_bytes(wrap["epk"], f"{what}.epk")
    if len(epk) != POINT_LEN or epk[0] != 0x04:
        raise _shape(f"{what}.epk must be a 65-byte uncompressed P-256 point")
    _exact(wrap["n"], NONCE_LEN, f"{what}.n")
    _exact(wrap["ct"], WRAPPED_LEN, f"{what}.ct")
    return dict(wrap)


def check_recovery_wrap(wrap, what: str = "wrap") -> dict:
    """{"alg": "recovery", "s": 16, "n": 12, "ct": 48}."""
    _keys(wrap, {"alg", "s", "n", "ct"}, what)
    if wrap["alg"] != "recovery":
        raise _shape(f"{what}: alg must be 'recovery'")
    _exact(wrap["s"], SALT_LEN, f"{what}.s")
    _exact(wrap["n"], NONCE_LEN, f"{what}.n")
    _exact(wrap["ct"], WRAPPED_LEN, f"{what}.ct")
    return dict(wrap)


PBKDF2_ITERATIONS = 650000
MAX_SEALS = 20


def check_seal(seal, what: str = "seal") -> dict:
    """Either a passkey seal — {"alg": "prf", "cred": <b64u credential id>,
    "s": 16, "n": 12, "ct": 48} — or a passphrase + Secret Key seal (§4b) —
    {"alg": "2skd", "kdf": "pbkdf2-sha256", "i": 650000, "s", "n", "ct"}.
    canopy-web never receives a passphrase, a Secret Key or a PRF output."""
    alg = seal.get("alg") if isinstance(seal, dict) else None
    if alg == "prf":
        _keys(seal, {"alg", "cred", "s", "n", "ct"}, what)
        if not b64u_bytes(seal["cred"], f"{what}.cred"):
            raise _shape(f"{what}.cred must name a passkey credential id")
    elif alg == "2skd":
        _keys(seal, {"alg", "kdf", "i", "s", "n", "ct"}, what)
        if seal["kdf"] != "pbkdf2-sha256":
            raise _shape(f"{what}: kdf must be 'pbkdf2-sha256'")
        if type(seal["i"]) is not int or seal["i"] != PBKDF2_ITERATIONS:
            raise _shape(f"{what}: i must be {PBKDF2_ITERATIONS}")
    else:
        raise _shape(f"{what}: alg must be 'prf' or '2skd'")
    _exact(seal["s"], SALT_LEN, f"{what}.s")
    _exact(seal["n"], NONCE_LEN, f"{what}.n")
    _exact(seal["ct"], WRAPPED_LEN, f"{what}.ct")
    return dict(seal)


# ---- who may hold a wrap ---------------------------------------------------------

def eligible_device_keys(agent) -> list:
    """ACTIVE device keys with an ECDH key, on runners whose proof this agent's
    resolve accepts — the devices that can be (and, at the current kv, should
    be) wrapped to."""
    from apps.harness import device_keys
    from apps.harness.models import RunnerDeviceKey

    return list(device_keys.keys_for_agent(agent)
                .filter(status=RunnerDeviceKey.ACTIVE).exclude(ecdh_jkt=""))


def eligible_members(agent) -> list:
    """Every current agent admin with a vault keyset, as UserVaultKeyset rows."""
    from apps.passkeys.models import UserVaultKeyset

    return [k for k in UserVaultKeyset.objects.select_related("user") if agent.is_admin(k.user)]


@dataclass
class _Target:
    kind: str
    device_key: object = None
    user: object = None

    @property
    def key(self) -> tuple:
        if self.kind == "device":
            return ("device", self.device_key.pk)
        if self.kind == "member":
            return ("member", self.user.pk)
        return ("recovery",)


def _wrap_key(w) -> tuple:
    if w.kind == "device":
        return ("device", w.device_key_id)
    if w.kind == "member":
        return ("member", w.user_id)
    return ("recovery",)


def _parse_wraps(agent, kv: int, wraps: list, *, allow_recovery: bool) -> list:
    """Validate `[{kind, target, wrap}]` and return unsaved AgentVaultWrap rows."""
    from .models import AgentVaultWrap

    devices = {str(k.pk): k for k in eligible_device_keys(agent)}
    members = {str(k.user_id): k for k in eligible_members(agent)}
    out, seen = [], set()
    for i, item in enumerate(wraps):
        what = f"wraps[{i}]"
        kind, target, wrap = item.kind, item.target, item.wrap
        if kind == "device":
            try:
                key_id = str(uuid.UUID(str(target)))
            except ValueError as exc:
                raise _shape(f"{what}: a device target is a device key id") from exc
            key = devices.get(key_id)
            if key is None:
                raise ZkError(422, "zk_invalid_target",
                              f"{what}: device key {key_id} is not an active device key with an ECDH "
                              f"key on a runner {agent.slug} routes to")
            row = AgentVaultWrap(agent=agent, kv=kv, kind=kind, device_key=key,
                                 wrap=check_ecdh_wrap(wrap, what))
        elif kind == "member":
            ks = members.get(str(target))
            if ks is None:
                raise ZkError(422, "zk_invalid_target",
                              f"{what}: user {target} is not a current admin of {agent.slug} "
                              "with a vault key (Settings → Vault key)")
            row = AgentVaultWrap(agent=agent, kv=kv, kind=kind, user=ks.user,
                                 wrap=check_ecdh_wrap(wrap, what))
        elif kind == "recovery":
            if not allow_recovery:
                raise ZkError(422, "zk_invalid_target",
                              f"{what}: the recovery wrap is set only by enable or rotate")
            if target not in (None, ""):
                raise _shape(f"{what}: a recovery wrap has no target")
            row = AgentVaultWrap(agent=agent, kv=kv, kind=kind, wrap=check_recovery_wrap(wrap, what))
        else:
            raise _shape(f"{what}: kind must be device, member or recovery")
        k = _wrap_key(row)
        if k in seen:
            raise _shape(f"{what}: a second wrap for the same target")
        seen.add(k)
        out.append(row)
    return out


def _caller_can_open(rows: list, user) -> bool:
    """A member wrap to the caller's own keyset, or a device wrap to a key on a
    runner the caller OWNS (the canopy CLI enabling as its device)."""
    for w in rows:
        if w.kind == "member" and w.user_id == user.pk:
            return True
        if w.kind == "device" and w.device_key.runner.owner_id == user.pk:
            return True
    return False


def _check_full_set(agent, rows: list, user) -> None:
    recovery = [w for w in rows if w.kind == "recovery"]
    if len(recovery) != 1:
        raise ZkError(422, "zk_recovery_required", "exactly one recovery wrap is required")
    if not _caller_can_open(rows, user):
        raise ZkError(422, "zk_caller_wrap_required",
                      "include a wrap you can open: a member wrap to your own vault key, or a device "
                      "wrap to a device key on a runner you own — else nobody you trust holds "
                      "the new vault key")


def _parse_values(values: list) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, v in enumerate(values):
        name = (v.name or "").strip()
        if not name:
            raise _shape(f"values[{i}]: name is required")
        if name in out:
            raise _shape(f"values[{i}]: {name} appears twice")
        out[name] = check_value_zk(v.value_zk, name)
    return out


def _require_coverage(agent, names) -> None:
    """Every stored credential must be in the body: a value left behind would be
    custodial plaintext (enable) or under a retired vault key (rotate)."""
    from .models import AgentCredential

    missing = sorted(set(AgentCredential.objects.filter(agent=agent).values_list("name", flat=True))
                     - set(names))
    if missing:
        raise ZkError(422, "zk_values_incomplete",
                      "every stored credential must be included; missing: " + ", ".join(missing))


def _write_values(agent, values: dict[str, str], kv: int, user) -> None:
    from .models import AgentCredential

    by = user if getattr(user, "is_authenticated", False) else None
    for name, value_zk in values.items():
        AgentCredential.objects.update_or_create(
            agent=agent, name=name,
            defaults={"value_zk": value_zk, "kv": kv, "value_enc": "", "updated_by": by},
        )


def _locked(agent):
    from .models import Agent

    return Agent.objects.select_for_update().get(pk=agent.pk)


def _require_enabled(agent) -> None:
    if not agent.zk_enabled:
        raise ZkError(409, "zk_not_enabled", f"{agent.slug} is not zero-knowledge")


def _save_wraps(rows: list, user) -> None:
    from .models import AgentVaultWrap

    by = user if getattr(user, "is_authenticated", False) else None
    for w in rows:
        w.created_by = by
    AgentVaultWrap.objects.bulk_create(rows)


# ---- the writes -------------------------------------------------------------------

def enable(agent, *, kv: int, wraps: list, values: list, user) -> None:
    """Turn zero-knowledge on, atomically: the wraps, every value as ciphertext
    (each stored value's custodial copy is wiped), then the flag."""
    from .models import AgentVaultWrap

    if kv != 1:
        raise ZkError(422, "zk_kv_mismatch", "enable starts the vault key at kv 1")
    with transaction.atomic():
        agent = _locked(agent)
        if agent.zk_enabled:
            raise ZkError(409, "zk_already_enabled", f"{agent.slug} is already zero-knowledge")
        rows = _parse_wraps(agent, kv, wraps, allow_recovery=True)
        # The CLI may enable as its device before the owner has a keyset; the
        # owner's member wrap is then granted later (decision, 2026-10-16).
        _check_full_set(agent, rows, user)
        vals = _parse_values(values)
        _require_coverage(agent, vals)
        AgentVaultWrap.objects.filter(agent=agent).delete()
        _save_wraps(rows, user)
        _write_values(agent, vals, kv, user)
        agent.zk_enabled, agent.zk_kv = True, kv
        agent.save(update_fields=["zk_enabled", "zk_kv", "updated_at"])


def set_values(agent, *, kv: int, values: list, user) -> list[str]:
    """Upsert ciphertext values at the current kv (non-clobbering)."""
    with transaction.atomic():
        agent = _locked(agent)
        _require_enabled(agent)
        if kv != agent.zk_kv:
            raise ZkError(409, "zk_kv_mismatch", f"the vault key is at kv {agent.zk_kv}, not {kv}")
        vals = _parse_values(values)
        _write_values(agent, vals, kv, user)
    return sorted(vals)


def add_wraps(agent, *, kv: int, wraps: list, user) -> int:
    """Grant: add (or replace) device/member wraps at the current kv."""
    from .models import AgentVaultWrap

    with transaction.atomic():
        agent = _locked(agent)
        _require_enabled(agent)
        if kv != agent.zk_kv:
            raise ZkError(409, "zk_kv_mismatch", f"the vault key is at kv {agent.zk_kv}, not {kv}")
        rows = _parse_wraps(agent, kv, wraps, allow_recovery=False)
        for w in rows:
            AgentVaultWrap.objects.filter(agent=agent, kv=kv, kind=w.kind, device_key=w.device_key,
                                          user=w.user).delete()
        _save_wraps(rows, user)
    return len(rows)


def rotate(agent, *, new_kv: int, wraps: list, values: list, user) -> None:
    """A fresh vault key: replace every wrap and every value atomically. Every
    target that holds a wrap now and still qualifies must get one at new_kv."""
    from .models import AgentVaultWrap

    with transaction.atomic():
        agent = _locked(agent)
        _require_enabled(agent)
        if new_kv != agent.zk_kv + 1:
            raise ZkError(409, "zk_kv_mismatch",
                          f"the vault key is at kv {agent.zk_kv}; rotate to kv {agent.zk_kv + 1}")
        rows = _parse_wraps(agent, new_kv, wraps, allow_recovery=True)
        _check_full_set(agent, rows, user)
        given = {_wrap_key(w) for w in rows}
        eligible = ({("device", k.pk) for k in eligible_device_keys(agent)}
                    | {("member", k.user_id) for k in eligible_members(agent)})
        current = {_wrap_key(w) for w in AgentVaultWrap.objects.filter(agent=agent, kv=agent.zk_kv)
                   if w.kind != "recovery"}
        missing = sorted(str(k[1]) for k in (current & eligible) - given)
        if missing:
            raise ZkError(422, "zk_rewrap_required",
                          "re-wrap for every remaining target; missing: " + ", ".join(missing))
        vals = _parse_values(values)
        _require_coverage(agent, vals)
        AgentVaultWrap.objects.filter(agent=agent).delete()
        _save_wraps(rows, user)
        _write_values(agent, vals, new_kv, user)
        agent.zk_kv = new_kv
        agent.save(update_fields=["zk_kv", "updated_at"])


def disable(agent, *, values: list, user) -> None:
    """Back to custodial storage: every value re-sealed from the plaintext the
    browser decrypted, every wrap deleted, the flag off."""
    from .models import AgentCredential, AgentVaultWrap

    with transaction.atomic():
        agent = _locked(agent)
        _require_enabled(agent)
        plain: dict[str, str] = {}
        for i, v in enumerate(values):
            name = (v.name or "").strip()
            if not name or name in plain:
                raise _shape(f"values[{i}]: a missing or repeated name")
            if not (v.plaintext or "").strip():
                raise _shape(f"values[{i}]: blank value for {name}")
            plain[name] = v.plaintext.strip()
        _require_coverage(agent, plain)
        by = user if getattr(user, "is_authenticated", False) else None
        for name, value in plain.items():
            AgentCredential.objects.update_or_create(
                agent=agent, name=name,
                defaults={"value_enc": AgentCredential.seal(agent, name, value), "value_zk": "",
                          "kv": None, "updated_by": by},
            )
        AgentVaultWrap.objects.filter(agent=agent).delete()
        agent.zk_enabled, agent.zk_kv = False, 0
        agent.save(update_fields=["zk_enabled", "zk_kv", "updated_at"])


def forget_device_wraps(device_key) -> int:
    """A device's ECDH key changed: its wraps were to the old key (and bound to
    the old thumbprint), so they are useless — drop them; it shows as pending."""
    from .models import AgentVaultWrap

    deleted, _ = AgentVaultWrap.objects.filter(device_key=device_key).delete()
    return deleted


def forget_member_wraps(user) -> int:
    """A person replaced their vault key: wraps to the old one are useless."""
    from .models import AgentVaultWrap

    deleted, _ = AgentVaultWrap.objects.filter(kind="member", user=user).delete()
    return deleted


# ---- reads ----------------------------------------------------------------------------

def _device_target(k) -> dict:
    return {"kind": "device", "target": str(k.pk), "runner": str(k.runner_id),
            "runner_name": k.runner.name, "label": k.label, "device_kind": k.kind,
            "ecdh_jkt": k.ecdh_jkt, "public_jwk": k.ecdh_public_jwk, "ecdh_public_jwk": k.ecdh_public_jwk}


def _member_target(ks) -> dict:
    return {"kind": "member", "target": str(ks.user_id), "email": ks.user.email,
            "public_jwk": ks.public_jwk}


def pending_targets(agent) -> list[dict]:
    """Eligible principals with no wrap at the current kv: who a member should
    grant to next. Before enable, every eligible principal (what enable wraps to)."""
    from .models import AgentVaultWrap

    have = ({_wrap_key(w) for w in AgentVaultWrap.objects.filter(agent=agent, kv=agent.zk_kv)}
            if agent.zk_enabled else set())
    out = [_device_target(k) for k in eligible_device_keys(agent) if ("device", k.pk) not in have]
    out += [_member_target(ks) for ks in eligible_members(agent) if ("member", ks.user_id) not in have]
    return out


def state(agent, *, include_values: bool = False) -> dict:
    """Everything a member's client needs to grant, rotate or recover. Wraps
    are public data; values are ciphertext and only included on request."""
    from .models import AgentCredential, AgentVaultWrap

    devices = {k.pk for k in eligible_device_keys(agent)}
    members = {ks.user_id: ks for ks in eligible_members(agent)}
    wraps = []
    for w in (AgentVaultWrap.objects.filter(agent=agent)
              .select_related("device_key__runner", "user", "created_by").order_by("kv", "kind", "id")):
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
        wraps.append(row)
    out = {
        "agent": agent.slug, "agent_id": str(agent.pk), "enabled": agent.zk_enabled,
        "kv": agent.zk_kv, "wraps": wraps, "pending": pending_targets(agent),
        # A wrap whose holder no longer qualifies (key revoked, admin removed)
        # still opens the vault key it was made for: only a rotation retires it.
        "rotation_recommended": any(not w["current"] for w in wraps if w["kind"] != "recovery"),
        "values": None,
    }
    if include_values:
        out["values"] = {c.name: c.value_zk for c in AgentCredential.objects.filter(agent=agent)
                         if c.is_zk}
    return out


def resolve_payload(agent, device_key, wanted: set[str] | None) -> tuple[dict, dict | None]:
    """`(values, wrap)` for a resolve by `device_key`: the ciphertexts (scoped by
    `?refs=` like a custodial resolve) and that device's wrap at the current kv,
    or None when it has none."""
    from . import services
    from .models import AgentCredential, AgentVaultWrap

    qs = AgentCredential.objects.filter(agent=agent).exclude(value_zk="")
    if wanted is not None:
        qs = qs.filter(name__in=sorted(set(wanted) & services.declared_credential_names(agent)))
    values = {c.name: c.value_zk for c in qs}
    w = AgentVaultWrap.objects.filter(agent=agent, kv=agent.zk_kv, kind="device",
                                      device_key=device_key).first()
    return values, (w.wrap if w is not None else None)


def device_wrap_missing(agent, request, device_key) -> None:
    """Tell the owner a box that should read this agent holds no wrap (409)."""
    import logging

    from . import vault_alerts

    try:
        vault_alerts.raise_alert(
            agent, "zk-wrap-missing", user=request.user, runner=device_key.runner,
            turn_id=request.META.get("HTTP_X_CANOPY_TURN_ID", "")[:120],
            detail=(f"{device_key.runner.name} ({device_key.label or device_key.kind}) proved itself "
                    f"but holds no vault-key wrap at kv {agent.zk_kv}"
                    + ("" if device_key.ecdh_jkt else " — it has no ECDH key; re-enroll it")
                    + ". Grant it from the agent's Credentials page.")[:500])
    except Exception:  # noqa: BLE001 - an alert hiccup must not turn a 409 into a 500
        logging.getLogger(__name__).exception("zk wrap-missing alert failed for %s", agent.slug)


# ---- the person's own keyset ------------------------------------------------------------

def put_keyset(user, *, public_jwk: dict, seals: list, replace: bool = False):
    """Create or extend the caller's keyset. Changing the PUBLIC key is a new
    identity: it needs `replace`, and drops every member wrap to the old one
    (those agents then list the person as pending)."""
    from apps.harness import device_keys
    from apps.passkeys.models import Passkey, UserVaultKeyset

    try:
        jwk = device_keys.normalize_public_jwk(public_jwk)
    except device_keys.KeyError_ as exc:
        raise _shape(f"public_jwk: {exc}") from exc
    if not seals:
        raise _shape("at least one seal is required")
    if len(seals) > MAX_SEALS:
        raise _shape(f"at most {MAX_SEALS} seals")
    mine = set(Passkey.objects.filter(user=user).values_list("credential_id", flat=True))
    checked, creds = [], set()
    for i, s in enumerate(seals):
        seal = check_seal(s, f"seals[{i}]")
        if seal["alg"] != "prf":
            checked.append(seal)
            continue
        if seal["cred"] not in mine:
            raise ZkError(422, "zk_unknown_passkey", f"seals[{i}]: not one of your passkeys")
        if seal["cred"] in creds:
            raise _shape(f"seals[{i}]: a second seal for the same passkey")
        creds.add(seal["cred"])
        checked.append(seal)
    with transaction.atomic():
        ks = UserVaultKeyset.objects.select_for_update().filter(user=user).first()
        if ks is not None and ks.public_jwk != jwk:
            if not replace:
                raise ZkError(409, "zk_keyset_exists",
                              "you already have a vault key; send replace: true to start a new one "
                              "(every agent must then grant you again)")
            forget_member_wraps(user)
        if ks is None:
            ks = UserVaultKeyset.objects.create(user=user, public_jwk=jwk, seals=checked)
        else:
            ks.public_jwk, ks.seals = jwk, checked
            ks.save(update_fields=["public_jwk", "seals", "updated_at"])
    return ks
