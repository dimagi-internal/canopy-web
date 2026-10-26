"""Zero-knowledge vault for an agent (Canopy Vault Phase 2, canopy-web#1550).

canopy-web stores only ciphertext and wrapped keys for an agent: its vault has
no other mode. Every byte format is defined by `docs/vault/zk-v1/zk_v1_reference.py`
(with test vectors beside it); this module never encrypts, decrypts or unwraps
anything. It checks SHAPES — the algorithm tag, base64url lengths, the `zk1:`
prefix — and enforces WHO may hold a wrap:

* a `device` wrap goes to an ACTIVE RunnerDeviceKey that has an ECDH key, on a
  runner this agent's resolve accepts proofs from (`runner_serves_agent`), whose
  PERSON (`vault_members.person_of_device`) is a member of the agent's vault —
  the machine layer never grants on its own;
* a `member` wrap goes to a member of the agent's vault (`vault_members`: direct,
  or linked — by default everyone with full access to the agent) who has a
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

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.harness import device_keys
from apps.harness.models import RunnerDeviceKey
from apps.passkeys.models import Passkey, UserVaultKeyset

from .. import services
from ..models import Agent, AgentCredential, AgentVaultWrap, VaultKeyWrap
from . import vault_alerts, vault_engine as engine, vault_members

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
    """A passkey seal — {"alg": "prf", "cred": <b64u credential id>,
    "s": 16, "n": 12, "ct": 48} —, a passphrase + Secret Key seal (§4b) —
    {"alg": "2skd", "kdf": "pbkdf2-sha256", "i": 650000, "s", "n", "ct"} — or the
    person's recovery-code seal (§4c) — {"alg": "recovery", "s", "n", "ct"}.
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
    elif alg == "recovery":
        # §4c: the person's ONE recovery code opens their key (and so every
        # agent they hold) — instead of a recovery code per agent.
        _keys(seal, {"alg", "s", "n", "ct"}, what)
    else:
        raise _shape(f"{what}: alg must be 'prf', '2skd' or 'recovery'")
    _exact(seal["s"], SALT_LEN, f"{what}.s")
    _exact(seal["n"], NONCE_LEN, f"{what}.n")
    _exact(seal["ct"], WRAPPED_LEN, f"{what}.ct")
    return dict(seal)


# ---- who may hold a wrap ---------------------------------------------------------

def eligible_device_keys(agent, facts=None) -> list:
    """ACTIVE device keys with an APPROVED ECDH key, on runners whose proof this
    agent's resolve accepts — the devices that can be (and, at the current kv,
    should be) wrapped to. A new ECDH key a person has not approved yet gets
    nothing, though its signing key stays active. `facts`: `device_keys.serving_facts`."""
    return list(eligible_device_keys_qs(agent, facts))


def eligible_device_keys_qs(agent, facts=None):
    """`eligible_device_keys` as a queryset, for a caller that only asks whether any exist."""

    ids = (facts.once(("vault-members", agent.pk), lambda: vault_members.agent_member_ids(agent)) if facts
           else vault_members.agent_member_ids(agent))
    return (device_keys.keys_for_agent(agent, facts)
            .filter(status=RunnerDeviceKey.ACTIVE, ecdh_approved_at__isnull=False)
            .exclude(ecdh_jkt="")
            .filter(Q(runner__owner_id__in=ids) | Q(runner__owner__isnull=True, approved_by_id__in=ids)))


def device_waits(agent, key) -> bool:
    """Is this active device key unable to read `agent`'s zero-knowledge values?

    kv 0 (born zero-knowledge, no vault key yet): when any stored value was not
    sealed to it. kv > 0: when it holds no wrap at the current kv. The one
    predicate behind "Give access" on the approve flow
    (`api_device_keys.zk_agents_waiting_on`) and the vault health of an agent
    (`vault_health.machines_waiting`)."""
    return key.pk in devices_waiting(agent, [key])


def devices_waiting(agent, keys) -> set:
    """The pks of those of `keys` that `device_waits` — asked of many keys in one
    query, for a screen listing every machine."""
    from .zk_seal import sealed_targets  # cycle: agents.zk_seal -> agents.zk  # noqa: PLC0415

    keys = [k for k in keys if k.ecdh_jkt]
    if not agent.zk_enabled or not keys:
        return set()
    if agent.zk_kv == 0:
        stored = [sealed_targets(v) for v in AgentCredential.objects.filter(agent=agent)
                  .exclude(value_zk="").exclude(account="delegated").values_list("value_zk", flat=True)]
        return {k.pk for k in keys if any(("device", k.ecdh_jkt) not in t for t in stored)}
    wrapped = set(AgentVaultWrap.objects.filter(agent=agent, kv=agent.zk_kv, kind="device",
                                                device_key__in=keys).values_list("device_key_id", flat=True))
    return {k.pk for k in keys if k.pk not in wrapped}


def eligible_members(agent) -> list:
    """Every member of the agent's vault with a vault keyset, as UserVaultKeyset rows."""

    return list(UserVaultKeyset.objects.filter(user_id__in=vault_members.agent_member_ids(agent))
                .select_related("user"))


def _wrap_key(w) -> tuple:
    return engine.wrap_key(w)


def has_recovery_seal(user_id) -> bool:
    """The person keeps ONE recovery code for their vault key (§4c)."""

    ks = UserVaultKeyset.objects.filter(user_id=user_id).first()
    return bool(ks) and any(s.get("alg") == "recovery" for s in ks.seals)


def value_digest(value_zk: str) -> str:
    """What a compare-and-swap `expect` names: hex SHA-256 of the stored text."""
    return engine.value_digest(value_zk)


def _locked(agent):

    return Agent.objects.select_for_update().get(pk=agent.pk)


def _require_enabled(agent) -> None:
    if not agent.zk_enabled:
        raise ZkError(409, "zk_not_enabled", f"{agent.slug} is not zero-knowledge")


def _store(agent) -> "engine.Store":
    """The agent's vault, for the rules in `vault_engine`: its rows live on the agent."""

    def save_kv(kv: int) -> None:
        agent.zk_enabled, agent.zk_kv = True, kv
        agent.save(update_fields=["zk_enabled", "zk_kv", "updated_at"])

    return engine.Store(
        label=agent.slug, kv=agent.zk_kv,
        wrap_model=AgentVaultWrap, secret_model=AgentCredential, owner_kw={"agent": agent},
        eligible_devices=lambda: eligible_device_keys(agent),
        eligible_members=lambda: eligible_members(agent),
        lock=lambda: _store(_locked(agent)), save_kv=save_kv,
        require_enabled=lambda: _require_enabled(agent),
        # A browser may seal a value to its recipients before the vault key exists.
        accepts_sealed=True,
        device_scope=f"on a runner {agent.slug} routes to, working for a member of {agent.slug}'s vault",
        member_scope=f"{agent.slug}'s vault",
    )


# ---- the writes (the rules are vault_engine's, shared with a workspace vault) ----------

def enable(agent, *, kv: int, wraps: list, values: list, user) -> None:
    """Create the agent's first vault key, atomically: the wraps, every stored
    value re-encrypted under it, then the flag. An agent is zero-knowledge from
    birth at kv 0, "no vault key yet" (`born_zk`); this moves it to kv 1. The CLI may enable as its device before the owner has a
    keyset; the owner's member wrap is then granted later (decision, 2026-10-16)."""
    with transaction.atomic():
        engine.enable(_store(agent), kv=kv, wraps=wraps, values=values, user=user,
                      already=f"{agent.slug} is already zero-knowledge")


def set_values(agent, *, kv: int, values: list, user) -> list[str]:
    """Upsert ciphertext values at the current kv (non-clobbering). A `zks1` value is
    legal here: a browser sealing to recipients before the vault key exists (kv 0),
    or one not yet settled."""
    return engine.set_values(_store(agent), kv=kv, values=values, user=user)


def add_wraps(agent, *, kv: int, wraps: list, user, only_new: bool = False) -> int:
    """Grant: add (or replace) device/member wraps at the current kv (`only_new`: a keeper)."""
    return engine.add_wraps(_store(agent), kv=kv, wraps=wraps, user=user, only_new=only_new)


def rotate(agent, *, new_kv: int, wraps: list, values: list, user) -> None:
    """A fresh vault key: replace every wrap and every value atomically."""
    engine.rotate(_store(agent), new_kv=new_kv, wraps=wraps, values=values, user=user)


def forget_device_wraps(device_key) -> int:
    """A device's ECDH key changed: its wraps were to the old key (and bound to
    the old thumbprint), so they are useless — drop them; it shows as pending."""

    deleted, _ = AgentVaultWrap.objects.filter(device_key=device_key).delete()
    more, _ = VaultKeyWrap.objects.filter(device_key=device_key).delete()
    return deleted + more


def forget_member_wraps(user) -> int:
    """A person replaced their vault key: wraps to the old one are useless — in
    agent vaults and workspace vaults alike, so each shows them as pending again."""
    deleted, _ = AgentVaultWrap.objects.filter(kind="member", user=user).delete()
    more, _ = VaultKeyWrap.objects.filter(kind="member", user=user).delete()
    return deleted + more


# ---- reads ----------------------------------------------------------------------------

def pending_targets(agent) -> list[dict]:
    """Eligible principals with no wrap at the current kv: who a member should
    grant to next. Before enable, every eligible principal (what enable wraps to)."""
    return engine.pending_targets(_store(agent), has_key=agent.zk_enabled)


def copies(agent) -> tuple[set[int], list[str]]:
    """Who holds the agent's CURRENT vault key and may pass it on now: the members with
    a copy, and the names of the machines (keepers) that hold one, serve the agent and
    work for a member. Empty before the vault has a key."""
    if not agent.zk_enabled or not agent.zk_kv:
        return set(), []
    members = vault_members.agent_member_ids(agent)
    rows = list(AgentVaultWrap.objects.filter(agent=agent, kv=agent.zk_kv, kind__in=["member", "device"])
                .select_related("device_key__runner"))
    people = {w.user_id for w in rows if w.kind == "member" and w.user_id in members}
    machines = sorted({w.device_key.runner.name for w in rows
                       if w.kind == "device" and w.device_key.status == w.device_key.ACTIVE
                       and vault_members.person_of_device(w.device_key) in members
                       and device_keys.runner_serves_agent(w.device_key.runner, agent)})
    return people, machines


def state(agent, *, include_values: bool = False) -> dict:
    """Everything a member's client needs to grant, rotate or recover. Wraps
    are public data; values are ciphertext and only included on request."""

    wraps = engine.wrap_rows(_store(agent))
    out = {
        "agent": agent.slug, "agent_id": str(agent.pk), "enabled": agent.zk_enabled,
        "kv": agent.zk_kv, "wraps": wraps, "pending": pending_targets(agent),
        "rotation_recommended": engine.rotation_recommended(wraps),
        "values": None,
        # Server-minted values sealed to recipients (`zks1`), waiting for a
        # vault-key holder to re-encrypt them as `zk1` — names only.
        "sealed": sorted(AgentCredential.objects.filter(agent=agent, value_zk__startswith="zks1:")
                         .exclude(account="delegated").values_list("name", flat=True)),
        # Credentials a person delegated: names only, never ciphertext — no member may
        # read one, and none is under the vault key to settle or rotate (`accounts`).
        "delegated": sorted(AgentCredential.objects.filter(agent=agent, account="delegated")
                            .values_list("name", flat=True)),
    }
    if include_values:
        out["values"] = {c.name: c.value_zk for c in AgentCredential.objects.filter(agent=agent)
                         .exclude(account="delegated")}
    return out


def needs_wrap(values: dict) -> bool:
    """A device can read `zks1` values with its own key; only `zk1` values need
    the vault key, so only they make a missing wrap fatal."""
    return any(v.startswith(ZK_PREFIX) for v in values.values())


def resolve_payload(agent, device_key, wanted: set[str] | None) -> tuple[dict, dict | None]:
    """`(values, wrap)` for a resolve by `device_key`: the ciphertexts (`zk1`
    under the vault key, or `zks1` sealed to recipients — scoped by `?refs=` to
    the agent's declared refs) and that device's wrap at the current kv, or None
    when it has none."""

    # A delegated entry never rides `values`: it goes only to a machine that runs the
    # agent, in the resolve's own `delegated` field (`accounts.resolve_payload`).
    qs = AgentCredential.objects.filter(agent=agent).exclude(value_zk="").exclude(account="delegated")
    if wanted is not None:
        qs = qs.filter(name__in=sorted(set(wanted) & services.declared_credential_names(agent)))
    values = {c.name: c.value_zk for c in qs}
    w = (AgentVaultWrap.objects.filter(agent=agent, kv=agent.zk_kv, kind="device",
                                       device_key=device_key).first() if agent.zk_kv else None)
    return values, (w.wrap if w is not None else None)


def device_wrap_missing(agent, request, device_key) -> None:
    """Tell the owner a box that should read this agent holds no wrap (409)."""
    import logging

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
    (those agents then list the person as pending).

    A login that is not a person writes one only as an agent's TEST vault key
    (`login_test_key.check_keyset_write`): first key only, passphrase seals only."""
    from . import login_test_key  # cycle: login_test_key -> zk (disable)  # noqa: PLC0415

    def _test_key_check(exists: bool) -> None:
        try:
            login_test_key.check_keyset_write(user, seals=seals or [], exists=exists)
        except login_test_key.NotAPerson as exc:
            raise ZkError(exc.status, exc.code, str(exc)) from exc

    _test_key_check(UserVaultKeyset.objects.filter(user=user).exists())
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
    algs = [s.get("alg") if isinstance(s, dict) else None for s in seals]
    if algs.count("recovery") > 1:
        raise _shape("at most one recovery seal")
    if all(a == "recovery" for a in algs):
        raise _shape("a recovery seal needs a passkey or passphrase seal beside it")
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
        _test_key_check(ks is not None)  # again under the lock: two first writes make one key
        if ks is not None and ks.public_jwk != jwk:
            if not replace:
                raise ZkError(409, "zk_keyset_exists",
                              "you already have a vault key; send replace: true to start a new one "
                              "(every agent must then grant you again)")
            forget_member_wraps(user)
        if ks is None:
            ks = UserVaultKeyset.objects.create(user=user, public_jwk=jwk, seals=checked)
        else:
            changed = ks.public_jwk != jwk
            ks.public_jwk, ks.seals = jwk, checked
            if changed:
                ks.replaced_at = timezone.now()
            ks.save(update_fields=["public_jwk", "seals", "replaced_at", "updated_at"])
    return ks
