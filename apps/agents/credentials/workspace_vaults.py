"""Workspace vaults: shared secrets a workspace keeps for its agents (spec
2026-10-22-vault-access-follows-canopy-access-design.md §4).

A workspace may hold any number of vaults (Jonathan, 2026-10-22). Each is always
zero-knowledge, in the same formats as an agent's vault (docs/vault/zk-v1/
zk_v1_reference.py), with the vault's `binding_id` ("vault:<uuid>") in the place an
agent's id takes in every AAD and HKDF info — so the browser and the canopy CLI encrypt
for either with the same code, and a copy made for one can never open the other.

* **Who may open it** is its membership (`vault_members`): by default the workspace's
  owners (`owner`) and everyone with full access to an agent it is attached to (`member`).
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
the same owner, serving the same tree or agent, the same sensitive-data standing.
"""
from __future__ import annotations


from django.db import transaction
from django.db.models import Q

from apps.harness import device_keys, runner_requirements
from apps.harness.models import Runner, RunnerDeviceKey
from apps.harness.runners import enrolled_roots
from apps.passkeys.models import UserVaultKeyset
from apps.workspaces import services as wsvc

from ..models import Agent, Vault, VaultAttachment, VaultKeyWrap, VaultLink, VaultSecret
from . import vault_engine as engine
from . import vault_members
from .zk import ZkError, check_value_zk


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
                                 role=vault_members.ADMIN, created_by=by)
        VaultLink.objects.create(vault=v, kind=VaultLink.ATTACHED, role=vault_members.MEMBER, created_by=by)
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
                          "owner, same tree or agent served, same sensitive-data standing), so they cannot share a vault")


def detach(vault: Vault, attachment_id: int) -> bool:
    deleted, _ = VaultAttachment.objects.filter(vault=vault, pk=attachment_id).delete()
    return bool(deleted)


def attached_agents(vault: Vault) -> list[Agent]:
    """Every agent this vault serves (`vault_members.attached_agents`)."""
    return vault_members.attached_agents(vault)


# ---- serving a runner -------------------------------------------------------------------

def isolation_boundary(runner: Runner) -> tuple:
    """What a runner may share a vault only inside: its owner, what it serves (the
    agent of a runner that runs one agent, the root of a runner that serves a tree, or —
    a runner that declares neither — the trees it is enrolled in, kept apart from
    declared ones), and whether it is a sensitive-data
    box. A box holds every value in every vault it opens, so two boxes share one only
    when either could already have held the other's work."""
    serves = runner.serves()
    if serves.get("kind") == "agent":
        where: tuple = ("agent", str(serves.get("slug") or ""))
    elif serves.get("kind") == "tree":
        where = ("tree", str(serves.get("root") or ""))
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
    of its FIRST attached runner. A runner whose tree or agent, owner or standing changed after it
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
        vault_members.add_member(v, runner.owner, role=vault_members.ADMIN, by=by)
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
    gog OAuth clients in dimagi/Shared), so the agent's own declaration need not repeat it."""
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


def pending_targets(vault: Vault) -> list[dict]:
    return engine.pending_targets(_store(vault), has_key=bool(vault.kv))


def state(vault: Vault, *, include_values: bool = False) -> dict:
    """The agent-vault `ZkStateOut` shape, so the browser and the CLI drive either:
    `agent` is the vault's name and `agent_id` its binding id."""
    wraps = engine.wrap_rows(_store(vault))
    out = {"agent": vault.name, "agent_id": binding_id(vault), "enabled": vault.kv > 0, "kv": vault.kv,
           "wraps": wraps, "pending": pending_targets(vault),
           "rotation_recommended": engine.rotation_recommended(wraps),
           "values": None, "sealed": []}
    if include_values:
        out["values"] = dict(VaultSecret.objects.filter(vault=vault).values_list("name", "value_zk"))
    return out


# ---- the writes (the agent vault's rules — `vault_engine` — for a workspace vault) ------

def _locked(vault: Vault) -> Vault:
    return Vault.objects.select_for_update().get(pk=vault.pk)


def _store(vault: Vault) -> "engine.Store":
    """A workspace vault, for the rules in `vault_engine`: its rows live on the vault."""

    def save_kv(kv: int) -> None:
        vault.kv = kv
        vault.save(update_fields=["kv"])

    return engine.Store(
        label=vault.name, kv=vault.kv,
        wrap_model=VaultKeyWrap, secret_model=VaultSecret, owner_kw={"vault": vault},
        eligible_devices=lambda: eligible_device_keys(vault),
        eligible_members=lambda: eligible_members(vault),
        lock=lambda: _store(_locked(vault)), save_kv=save_kv,
        device_scope=f"on a runner serving an agent {vault.name} is attached to, working for a member",
        member_scope=vault.name,
    )


def enable(vault: Vault, *, kv: int, wraps: list, values: list, user) -> None:
    """Make the vault's first key (kv 1): its copies and any first values. Needs a copy
    the caller can open, and a way back (a recovery wrap, or a member whose own key has
    a recovery code)."""
    with transaction.atomic():
        engine.enable(_store(vault), kv=kv, wraps=wraps, values=values, user=user,
                      already=f"{vault.name} already has a vault key")


def set_values(vault: Vault, *, kv: int, values: list, user) -> list[str]:
    return engine.set_values(_store(vault), kv=kv, values=values, user=user)


def set_runner_values(vault: Vault, *, kv: int, values: dict[str, str], user) -> list[str]:
    """`set_values` for a box writing its own credentials (`runners.store_vault_values`
    decides which names it may): the same lock, key-version check and `zk1:` shape."""
    with transaction.atomic():
        store = _store(vault).lock()
        if not store.kv:
            raise ZkError(409, "zk_no_vault_key", f"{vault.name} has no vault key yet")
        if kv != store.kv:
            raise ZkError(409, "zk_kv_mismatch", f"the vault key is at kv {store.kv}, not {kv}")
        vals = {name: check_value_zk(value, name) for name, value in values.items()}
        engine.write_values(store, vals, kv, user)
    return sorted(vals)


def add_wraps(vault: Vault, *, kv: int, wraps: list, user, only_new: bool = False) -> int:
    """`zk.add_wraps` for a workspace vault, `only_new` included."""
    return engine.add_wraps(_store(vault), kv=kv, wraps=wraps, user=user, only_new=only_new)


def rotate(vault: Vault, *, new_kv: int, wraps: list, values: list, user) -> None:
    """A fresh key for the vault, as an agent's vault rotates: every copy and every value
    replaced at once, and every current holder who still qualifies re-wrapped — so a
    member or machine removed since keeps a copy that opens nothing written after."""
    engine.rotate(_store(vault), new_kv=new_kv, wraps=wraps, values=values, user=user)


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

