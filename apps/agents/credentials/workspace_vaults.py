"""Workspace vaults: shared secrets a workspace keeps for its agents (spec
2026-10-22-vault-access-follows-canopy-access-design.md §4).

A workspace may hold any number of vaults (Jonathan, 2026-10-22). Each is always
zero-knowledge, in the same formats as an agent's vault (docs/vault/zk-v1/
zk_v1_reference.py), with the vault's `binding_id` ("vault:<uuid>") in the place an
agent's id takes in every AAD and HKDF info — so the browser and the canopy CLI encrypt
for either with the same code, and a copy made for one can never open the other.

* **Who may open it** is its membership (`vault_members`): by default the workspace's
  owners (`manage`) and everyone with full access to an agent it is attached to (`use`).
* **Which agents it serves** are its attachments: one agent, or every agent in a
  workspace and below it. A turn reads every vault attached to its agent, layered under
  the agent's own vault (`layers_for`).
* **Who may hold its key:** a member with a vault key of their own; a machine whose
  person is a member AND that serves an agent the vault is attached to — or that is a
  RUNNER the vault is attached to, inside one isolation boundary (below).

A vault attached to a cloud runner holds that box's own Claude credentials (spec
2026-10-23-runner-claude-credentials-in-the-vault-design.md): the box opens it with its
own device key, through `GET /api/harness/runners/{id}/credential/resolve`. Several
runners may share one vault only inside one **isolation boundary** (`isolation_boundary`):
the same owner, the same isolation unit, the same sensitive-data standing.
"""
from __future__ import annotations

import uuid

from django.db import transaction
from django.db.models import Q

from apps.harness import device_keys, runner_requirements
from apps.harness.models import Runner, RunnerDeviceKey
from apps.harness.runners import enrolled_roots
from apps.passkeys.models import UserVaultKeyset
from apps.workspaces import services as wsvc

from ..models import Agent, Vault, VaultAttachment, VaultKeyWrap, VaultLink, VaultSecret
from . import vault_members, zk
from .zk import ZkError, _shape, check_ecdh_wrap, check_recovery_wrap, check_value_zk


def binding_id(vault: Vault) -> str:
    """What a workspace vault's ciphertext and key copies are bound to."""
    return f"vault:{vault.pk}"


# ---- making one, and what it serves ----------------------------------------------------

def create(workspace, name: str, *, by) -> Vault:
    name = (name or "").strip()
    if not name:
        raise ZkError(422, "vault_bad_name", "a vault needs a name")
    if Vault.objects.filter(workspace=workspace, kind=Vault.WORKSPACE, name=name).exists():
        raise ZkError(409, "vault_exists", f"{workspace.slug} already has a vault called {name}")
    with transaction.atomic():
        v = Vault.objects.create(workspace=workspace, kind=Vault.WORKSPACE, name=name, created_by=by)
        VaultLink.objects.create(vault=v, kind=VaultLink.WORKSPACE_OWNERS, workspace=workspace,
                                 role=vault_members.MANAGE, created_by=by)
        VaultLink.objects.create(vault=v, kind=VaultLink.ATTACHED, role=vault_members.USE, created_by=by)
    return v


def attach(vault: Vault, *, agent: Agent | None = None, workspace=None, runner: Runner | None = None,
           by=None) -> VaultAttachment:
    """Serve one agent, every agent in `workspace` and below, or one cloud runner. None
    may lie outside the vault's own workspace tree below it, and a runner must share an
    isolation boundary with every runner the vault already serves."""
    allowed = {vault.workspace_id} | wsvc.descendant_slugs({vault.workspace_id})
    if sum(x is not None for x in (agent, workspace, runner)) != 1:
        raise ZkError(422, "vault_bad_attachment", "attach to exactly one agent, workspace or runner")
    if runner is not None:
        _check_runner(vault, runner, allowed)
        where = runner.workspace_id
    else:
        where = agent.workspace_id if agent is not None else workspace.slug
    if where not in allowed:
        raise ZkError(422, "vault_bad_attachment",
                      f"{where} is not {vault.workspace_id} or a workspace below it")
    row, _ = VaultAttachment.objects.get_or_create(vault=vault, agent=agent, workspace=workspace,
                                                   runner=runner, defaults={"created_by": by})
    return row


def _check_runner_itself(runner: Runner) -> None:
    if (runner.kind != Runner.CLOUD or runner.status == Runner.RETIRED or not runner.owner_id
            or not runner.workspace_id):
        raise ZkError(422, "vault_bad_attachment",
                      f"{runner.name} is not a live cloud runner with an owner and a workspace; only "
                      "those keep their Claude credentials in a vault")


def _check_runner(vault: Vault, runner: Runner, allowed: set) -> None:
    _check_runner_itself(runner)
    if runner.workspace_id not in allowed:
        raise ZkError(422, "vault_bad_attachment",
                      f"{runner.name} lives in {runner.workspace_id}, not {vault.workspace_id} or below")
    mine = isolation_boundary(runner)
    for other in attached_runners(vault):
        if other.pk != runner.pk and isolation_boundary(other) != mine:
            raise ZkError(422, "vault_crosses_isolation",
                          f"{runner.name} and {other.name} are not in one isolation boundary (same "
                          "owner, same unit, same sensitive-data standing), so they cannot share a vault")


def detach(vault: Vault, attachment_id: int) -> bool:
    deleted, _ = VaultAttachment.objects.filter(vault=vault, pk=attachment_id).delete()
    return bool(deleted)


def attached_agents(vault: Vault) -> list[Agent]:
    """Every agent this vault serves (`vault_members.attached_agents`)."""
    return vault_members.attached_agents(vault)


# ---- serving a runner -------------------------------------------------------------------

def isolation_boundary(runner: Runner) -> tuple:
    """What a runner may share a vault only inside: its owner, its isolation unit (an
    agent unit's agent, a tree unit's root, or — a box that declares no unit — the trees
    it is enrolled in, kept apart from declared units), and whether it is a sensitive-data
    box. A box holds every value in every vault it opens, so two boxes share one only
    when either could already have held the other's work."""
    unit = runner.unit()
    if unit.get("kind") == "agent":
        where: tuple = ("agent", str(unit.get("slug") or ""))
    elif unit.get("kind") == "tree":
        where = ("tree", str(unit.get("root") or ""))
    else:
        where = ("box", tuple(enrolled_roots(runner)))
    return (runner.owner_id, where, runner_requirements.is_sensitive_data_box(runner))


def attached_runners(vault: Vault) -> list[Runner]:
    """Every live runner this vault serves, oldest attachment first."""
    return [a.runner for a in (VaultAttachment.objects.filter(vault=vault, runner__isnull=False)
                               .exclude(runner__status=Runner.RETIRED)
                               .select_related("runner").order_by("created_at", "pk"))]


def serving_runners(vault: Vault) -> list[Runner]:
    """The attached runners the vault actually serves: those still inside the boundary
    of its FIRST attached runner. A box whose unit, owner or standing changed after it
    was attached falls out until it is
    detached or put back: its copy of the key is then handed out to nobody."""
    runners = attached_runners(vault)
    if not runners:
        return []
    first = isolation_boundary(runners[0])
    return [r for r in runners if isolation_boundary(r) == first]


def runner_vaults(runner: Runner) -> list[Vault]:
    """The vaults that hold this runner's credentials and still serve it, oldest first."""
    out = []
    for a in (VaultAttachment.objects.filter(runner=runner, vault__kind=Vault.WORKSPACE)
              .select_related("vault").order_by("created_at", "pk")):
        if any(r.pk == runner.pk for r in serving_runners(a.vault)):
            out.append(a.vault)
    return out


def runner_vault_name(runner: Runner) -> str:
    return f"{runner.name} Claude credentials"


def create_for_runner(runner: Runner, *, by) -> Vault:
    """The runner's OWN vault (the default; sharing is a later, explicit attach): made in
    the runner's workspace with the standard links, its owner a direct `manage` member,
    and attached to it. Returns the vault it already has, if any."""
    have = runner_vaults(runner)
    if have:
        return have[0]
    _check_runner_itself(runner)
    with transaction.atomic():
        v = create(runner.workspace, runner_vault_name(runner), by=by)
        vault_members.add_member(v, runner.owner, role=vault_members.MANAGE, by=by)
        VaultAttachment.objects.create(vault=v, runner=runner, created_by=by)
    return v


def layers_for(agent: Agent) -> list[Vault]:
    """The workspace vaults a turn of `agent` reads, in layering order (a later layer
    wins a name clash): vaults attached to a workspace above-or-at the agent's, root
    first; then vaults attached to the agent itself, oldest first. The agent's own
    vault is the top layer, outside this list."""
    if not agent.workspace_id:
        return []
    chain = [agent.workspace_id] + list(agent.workspace.ancestor_slugs())  # nearest first
    depth = {slug: i for i, slug in enumerate(chain)}
    by_tree = (VaultAttachment.objects.filter(workspace_id__in=chain, vault__kind=Vault.WORKSPACE)
               .select_related("vault"))
    direct = (VaultAttachment.objects.filter(agent=agent, vault__kind=Vault.WORKSPACE)
              .select_related("vault").order_by("created_at"))
    out, seen = [], set()
    for a in sorted(by_tree, key=lambda a: (-depth[a.workspace_id], a.created_at)):
        if a.vault_id not in seen:
            seen.add(a.vault_id)
            out.append(a.vault)
    for a in direct:
        if a.vault_id not in seen:
            seen.add(a.vault_id)
            out.append(a.vault)
    return out


def layered_names(agent: Agent) -> set[str]:
    """Every secret name a workspace vault layered under `agent` holds. A turn asking for
    one of these is asking for a value the WORKSPACE supplies to all its agents (the fleet's
    gog OAuth clients in dimagi/Shared), so the agent's own runtime.yaml need not repeat it."""
    vaults = [v.pk for v in layers_for(agent)]
    return set(VaultSecret.objects.filter(vault_id__in=vaults).values_list("name", flat=True))


# ---- who may hold its key --------------------------------------------------------------

def eligible_members(vault: Vault) -> list:
    return list(UserVaultKeyset.objects.filter(user_id__in=vault_members.member_ids(vault))
                .select_related("user"))


def eligible_device_keys(vault: Vault) -> list:
    """Active keys with an approved ECDH key, on a runner that serves an agent this
    vault is attached to, whose person is a member of the vault."""
    ids = vault_members.member_ids(vault)
    served: set = set()
    for agent in attached_agents(vault):
        served |= set(device_keys.keys_for_agent(agent).values_list("pk", flat=True))
    runners = serving_runners(vault)
    if runners:
        served |= set(RunnerDeviceKey.objects.filter(runner__in=runners).values_list("pk", flat=True))
    return list(RunnerDeviceKey.objects.filter(pk__in=served, status=RunnerDeviceKey.ACTIVE,
                                               ecdh_approved_at__isnull=False)
                .exclude(ecdh_jkt="")
                .filter(Q(runner__owner_id__in=ids) | Q(runner__owner__isnull=True, approved_by_id__in=ids))
                .select_related("runner"))


def device_may_hold(vault: Vault, device_key) -> bool:
    """May this ONE machine hold the vault's key now? Its person is a member and its
    runner serves an agent the vault is attached to, or is a runner the vault serves —
    `eligible_device_keys`, asked of one key instead of listing every runner."""
    if vault_members.person_of_device(device_key) not in vault_members.member_ids(vault):
        return False
    if any(r.pk == device_key.runner_id for r in serving_runners(vault)):
        return True
    return any(device_keys.runner_serves_agent(device_key.runner, a) for a in attached_agents(vault))


def copies(vault: Vault) -> tuple[set[int], list[str]]:
    """`zk.copies`, for a workspace vault."""
    if not vault.kv:
        return set(), []
    members = vault_members.member_ids(vault)
    rows = list(VaultKeyWrap.objects.filter(vault=vault, kv=vault.kv, kind__in=["member", "device"])
                .select_related("device_key__runner"))
    people = {w.user_id for w in rows if w.kind == "member" and w.user_id in members}
    machines = sorted({w.device_key.runner.name for w in rows
                       if w.kind == "device" and w.device_key.status == RunnerDeviceKey.ACTIVE
                       and device_may_hold(vault, w.device_key)})
    return people, machines


def _key(w) -> tuple:
    if w.kind == "device":
        return ("device", w.device_key_id)
    if w.kind == "member":
        return ("member", w.user_id)
    return ("recovery",)


def pending_targets(vault: Vault) -> list[dict]:
    have = {_key(w) for w in VaultKeyWrap.objects.filter(vault=vault, kv=vault.kv)} if vault.kv else set()
    out = [{"kind": "device", "target": str(k.pk), "runner": str(k.runner_id), "runner_name": k.runner.name,
            "label": k.label, "device_kind": k.kind, "ecdh_jkt": k.ecdh_jkt, "public_jwk": k.ecdh_public_jwk,
            "ecdh_public_jwk": k.ecdh_public_jwk}
           for k in eligible_device_keys(vault) if ("device", k.pk) not in have]
    out += [{"kind": "member", "target": str(ks.user_id), "email": ks.user.email, "public_jwk": ks.public_jwk}
            for ks in eligible_members(vault) if ("member", ks.user_id) not in have]
    return out


def state(vault: Vault, *, include_values: bool = False) -> dict:
    """The agent-vault `ZkStateOut` shape, so the browser and the CLI drive either:
    `agent` is the vault's name and `agent_id` its binding id."""
    devices = {k.pk for k in eligible_device_keys(vault)}
    members = {ks.user_id: ks for ks in eligible_members(vault)}
    wraps = []
    for w in (VaultKeyWrap.objects.filter(vault=vault)
              .select_related("device_key__runner", "user", "created_by").order_by("kv", "kind", "id")):
        row = {"kind": w.kind, "kv": w.kv, "wrap": w.wrap, "created_at": w.created_at,
               "created_by_email": w.created_by.email if w.created_by_id else "",
               "target": None, "label": "", "current": True}
        if w.kind == "device":
            k = w.device_key
            row.update(target=str(k.pk), label=f"{k.runner.name} · {k.label or k.kind}", ecdh_jkt=k.ecdh_jkt,
                       current=k.pk in devices, public_jwk=k.ecdh_public_jwk or None,
                       ecdh_public_jwk=k.ecdh_public_jwk or None)
        elif w.kind == "member":
            ks = members.get(w.user_id)
            row.update(target=str(w.user_id), label=w.user.email, current=ks is not None,
                       public_jwk=ks.public_jwk if ks is not None else None)
        wraps.append(row)
    out = {"agent": vault.name, "agent_id": binding_id(vault), "enabled": vault.kv > 0, "kv": vault.kv,
           "wraps": wraps, "pending": pending_targets(vault),
           "rotation_recommended": any(not w["current"] for w in wraps if w["kind"] != "recovery"),
           "values": None, "sealed": []}
    if include_values:
        out["values"] = dict(VaultSecret.objects.filter(vault=vault).values_list("name", "value_zk"))
    return out


# ---- the writes (the agent vault's rules, for a workspace vault) -----------------------------

def _locked(vault: Vault) -> Vault:
    return Vault.objects.select_for_update().get(pk=vault.pk)


def _parse_wraps(vault: Vault, kv: int, wraps: list, *, allow_recovery: bool) -> list[VaultKeyWrap]:
    devices = {str(k.pk): k for k in eligible_device_keys(vault)}
    members = {str(k.user_id): k for k in eligible_members(vault)}
    out, seen = [], set()
    for i, item in enumerate(wraps):
        what = f"wraps[{i}]"
        if item.kind == "device":
            try:
                key = devices.get(str(uuid.UUID(str(item.target))))
            except ValueError as exc:
                raise _shape(f"{what}: a device target is a device key id") from exc
            if key is None:
                raise ZkError(422, "zk_invalid_target",
                              f"{what}: device key {item.target} is not an active device key on a runner "
                              f"serving an agent {vault.name} is attached to, working for a member")
            row = VaultKeyWrap(vault=vault, kv=kv, kind="device", device_key=key,
                               wrap=check_ecdh_wrap(item.wrap, what))
        elif item.kind == "member":
            ks = members.get(str(item.target))
            if ks is None:
                raise ZkError(422, "zk_invalid_target",
                              f"{what}: user {item.target} is not a member of {vault.name} with a vault key")
            row = VaultKeyWrap(vault=vault, kv=kv, kind="member", user=ks.user,
                               wrap=check_ecdh_wrap(item.wrap, what))
        elif item.kind == "recovery":
            if not allow_recovery:
                raise ZkError(422, "zk_invalid_target", f"{what}: the recovery wrap is set only by enable")
            row = VaultKeyWrap(vault=vault, kv=kv, kind="recovery", wrap=check_recovery_wrap(item.wrap, what))
        else:
            raise _shape(f"{what}: kind must be device, member or recovery")
        if _key(row) in seen:
            raise _shape(f"{what}: a second wrap for the same target")
        seen.add(_key(row))
        out.append(row)
    return out


def _caller_can_open(rows: list, user) -> bool:
    return any((w.kind == "member" and w.user_id == user.pk)
               or (w.kind == "device" and w.device_key.runner.owner_id == user.pk) for w in rows)


def _values(values: list) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, v in enumerate(values):
        name = (v.name or "").strip()
        if not name or name in out:
            raise _shape(f"values[{i}]: a name is required, once")
        out[name] = check_value_zk(v.value_zk, name)
    return out


def _write(vault: Vault, values: dict[str, str], kv: int, user) -> None:
    by = user if getattr(user, "is_authenticated", False) else None
    for name, value_zk in values.items():
        VaultSecret.objects.update_or_create(vault=vault, name=name,
                                             defaults={"value_zk": value_zk, "kv": kv, "updated_by": by})


def _save(rows: list, user) -> None:
    by = user if getattr(user, "is_authenticated", False) else None
    for w in rows:
        w.created_by = by
    VaultKeyWrap.objects.bulk_create(rows)


def enable(vault: Vault, *, kv: int, wraps: list, values: list, user) -> None:
    """Make the vault's first key (kv 1): its copies and any first values. Needs a copy
    the caller can open, and a way back (a recovery wrap, or a member whose own key has
    a recovery code)."""
    if kv != 1:
        raise ZkError(422, "zk_kv_mismatch", "enable starts the vault key at kv 1")
    with transaction.atomic():
        vault = _locked(vault)
        if vault.kv:
            raise ZkError(409, "zk_already_enabled", f"{vault.name} already has a vault key")
        rows = _parse_wraps(vault, kv, wraps, allow_recovery=True)
        if not (any(w.kind == "recovery" for w in rows)
                or any(w.kind == "member" and zk.has_recovery_seal(w.user_id) for w in rows)):
            raise ZkError(422, "zk_recovery_required",
                          "include a recovery wrap, or a member wrap to someone whose vault key has a "
                          "recovery code")
        if not _caller_can_open(rows, user):
            raise ZkError(422, "zk_caller_wrap_required", "include a wrap you can open")
        _save(rows, user)
        _write(vault, _values(values), kv, user)
        vault.kv = kv
        vault.save(update_fields=["kv"])


def set_values(vault: Vault, *, kv: int, values: list, user) -> list[str]:
    with transaction.atomic():
        vault = _locked(vault)
        if not vault.kv:
            raise ZkError(409, "zk_no_vault_key", f"{vault.name} has no vault key yet")
        if kv != vault.kv:
            raise ZkError(409, "zk_kv_mismatch", f"the vault key is at kv {vault.kv}, not {kv}")
        vals = _values(values)
        _write(vault, vals, kv, user)
    return sorted(vals)


def add_wraps(vault: Vault, *, kv: int, wraps: list, user, only_new: bool = False) -> int:
    """`zk.add_wraps` for a workspace vault, `only_new` included."""
    with transaction.atomic():
        vault = _locked(vault)
        if not vault.kv:
            raise ZkError(409, "zk_no_vault_key", f"{vault.name} has no vault key yet to grant")
        if kv != vault.kv:
            raise ZkError(409, "zk_kv_mismatch", f"the vault key is at kv {vault.kv}, not {kv}")
        rows = _parse_wraps(vault, kv, wraps, allow_recovery=False)
        for w in rows:
            held = VaultKeyWrap.objects.filter(vault=vault, kv=kv, kind=w.kind, device_key=w.device_key,
                                               user=w.user)
            if only_new and held.exists():
                raise ZkError(409, "zk_not_pending",
                              f"{w.kind} {w.device_key_id or w.user_id} already holds a copy — a keeper "
                              "only adds copies, never replaces one")
            held.delete()
        _save(rows, user)
    return len(rows)


def remove_secret(vault: Vault, name: str) -> bool:
    deleted, _ = VaultSecret.objects.filter(vault=vault, name=name).delete()
    return bool(deleted)


# ---- reads by a machine, and removal ------------------------------------------------------

def layers_payload(agent: Agent, device_key, wanted: set[str] | None) -> list[dict]:
    """For a resolve by `device_key`: each workspace vault layered under `agent`'s own,
    in order — its binding id, kv, this device's copy of its key, and its ciphertext
    (scoped by `?refs=` when the caller asked for names). A vault this device cannot
    open (no copy, or its person is not a member) is listed with `wrap: null` and no
    values, so the client can say which layer it is missing."""
    out = []
    for v in layers_for(agent):
        if not v.kv:
            continue
        mine = None
        # Checked on every read: a copy this device still holds opens nothing once
        # its person has left the vault.
        if device_key is not None and vault_members.person_of_device(device_key) in vault_members.member_ids(v):
            mine = VaultKeyWrap.objects.filter(vault=v, kv=v.kv, kind="device", device_key=device_key).first()
        qs = VaultSecret.objects.filter(vault=v)
        if wanted is not None:
            qs = qs.filter(name__in=sorted(wanted))
        out.append({"vault_id": str(v.pk), "name": v.name, "workspace": v.workspace_id,
                    "binding_id": binding_id(v), "kv": v.kv, "wrap": mine.wrap if mine else None,
                    "values": dict(qs.values_list("name", "value_zk")) if mine else {}})
    return out


def runner_layers_payload(runner: Runner, device_key, wanted: set[str] | None) -> list[dict]:
    """`layers_payload` for a runner's own read: each vault that holds `runner`'s
    credentials, oldest first, with this device's copy of its key and its ciphertext
    (only the names in `wanted`, when given). The same shape, so `canopy` opens it with
    the code that opens an agent's layers."""
    out = []
    for v in runner_vaults(runner):
        if not v.kv:
            continue
        mine = None
        if device_key is not None and vault_members.person_of_device(device_key) in vault_members.member_ids(v):
            mine = VaultKeyWrap.objects.filter(vault=v, kv=v.kv, kind="device", device_key=device_key).first()
        qs = VaultSecret.objects.filter(vault=v)
        if wanted is not None:
            qs = qs.filter(name__in=sorted(wanted))
        out.append({"vault_id": str(v.pk), "name": v.name, "workspace": v.workspace_id,
                    "binding_id": binding_id(v), "kv": v.kv, "wrap": mine.wrap if mine else None,
                    "values": dict(qs.values_list("name", "value_zk")) if mine else {}})
    return out


def runner_secret_names(runner: Runner) -> set[str]:
    """Every secret name a vault serving `runner` holds."""
    vaults = [v.pk for v in runner_vaults(runner)]
    return set(VaultSecret.objects.filter(vault_id__in=vaults).values_list("name", flat=True))

