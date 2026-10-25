"""Credential sets: one ordered list of Claude credentials that several cloud runners
read as their own (`RunnerCredentialSet`; docs/architecture/runners.md § Credential sets).

A set's LIST is shared by every runner on it, in whatever workspace tree each one
serves: an entry added, renamed, reordered or removed from any member's Credentials
tab changes it for all of them (`runners.list_owner`). Its VALUES are not shared
across trees. A vault never serves two isolation boundaries
(`workspace_vaults.isolation_boundary`), so a set keeps ONE VAULT PER WORKSPACE TREE
(`vault_in_tree`): the first runner of the set in a tree brings or gets that tree's
vault, every later one in the tree is attached to it, and each tree's vault holds its
own copy of each value — sealed by a person of that tree's vault in their browser, or
by one of its boxes after a sign-in. A runner of the set reads the list's names and
its own tree's values, so an entry with no value in its tree is one it skips.

How a runner comes onto a set:
* a fleet member, as it joins (`fleets.join` → `attach_member`);
* `share` — a cloud runner's own list becomes a new set, its entries and vault with it;
* `join` — a cloud runner with no list of its own reads an existing set.
Every runner on a set and the set itself have one owner: an isolation boundary is
one person's, so a set that crossed people could never share a vault anyway.
"""
from __future__ import annotations

from django.db import transaction

from apps.workspaces import services as wsvc

from . import runners as harness_runners
from .models import Runner, RunnerCredentialSet, RunnerLogin


class SetRefused(ValueError):
    """A set change that may not happen (see the message); `status` is the HTTP code."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _root(slug: str) -> str:
    return wsvc.tree_roots({slug}).get(slug, slug)


def vault_in_tree(cset: RunnerCredentialSet, workspace_slug: str):
    """The set's vault for the workspace tree `workspace_slug` is in, or None."""
    root = _root(workspace_slug)
    vaults = list(cset.vaults.all())
    roots = wsvc.tree_roots({v.workspace_id for v in vaults})
    return next((v for v in vaults if roots.get(v.workspace_id, v.workspace_id) == root), None)


def vaults_of(cset: RunnerCredentialSet) -> list:
    return list(cset.vaults.order_by("created_at"))


def attach_member(cset: RunnerCredentialSet, runner: Runner, *, by):
    """Serve `runner` from the set's vault for its tree: attach it to that vault, or —
    the set's first runner in the tree — make that vault (or adopt the runner's own,
    whose key it may already hold). The runner is then served by that vault ONLY: a
    second vault would hold values for names the list no longer has, and would be
    where a pasted value went first (`runners.open_runner_vault`).

    Raises `ZkError` when the vault may not serve it (another isolation boundary in
    the same tree: an agent unit beside a tree unit, a sensitive-data box beside one
    that is not); nothing is attached then."""
    from apps.agents.credentials import workspace_vaults  # noqa: PLC0415 — it imports harness.runners
    from apps.agents.models import VaultAttachment  # noqa: PLC0415 — with it

    with transaction.atomic():
        RunnerCredentialSet.objects.select_for_update().filter(pk=cset.pk).first()
        vault = vault_in_tree(cset, runner.workspace_id)
        if vault is None:
            own = workspace_vaults.runner_vaults(runner)
            vault = own[0] if own else _new_vault(cset, runner, by=by)
            cset.vaults.add(vault)
        else:
            workspace_vaults.attach(vault, runner=runner, by=by)
        VaultAttachment.objects.filter(runner=runner).exclude(vault=vault).delete()
    return vault


def _new_vault(cset: RunnerCredentialSet, runner: Runner, *, by):
    """The set's vault for `runner`'s tree, made attached to it and named for the set
    (unless its workspace already has a vault of that name)."""
    from apps.agents.credentials import workspace_vaults  # noqa: PLC0415 — it imports harness.runners
    from apps.agents.models import Vault  # noqa: PLC0415 — with it

    vault = workspace_vaults.create_for_runner(runner, by=by)
    if not Vault.objects.filter(workspace_id=vault.workspace_id, kind=Vault.WORKSPACE, name=cset.name).exists():
        vault.name = cset.name
        vault.save(update_fields=["name"])
    return vault


def share(runner: Runner, *, name: str, by) -> RunnerCredentialSet:
    """`runner`'s own list becomes a new set, named `name`: its tokens and API keys move
    onto the set in their order, and its vault becomes the set's vault for its tree, so
    nothing it reads changes. Other runners then `join` it. Refused for a runner already
    on a set, and while its list holds a full login (a login's refresh token rotates,
    so it cannot be shared — remove it first)."""
    name = (name or "").strip()[:100]
    if not name:
        raise SetRefused(422, "a credential set needs a name")
    if runner.credential_set_id:
        raise SetRefused(409, f"{runner.name} already reads a credential set")
    if runner.kind != Runner.CLOUD or not runner.owner_id:
        raise SetRefused(422, f"{runner.name} is not a cloud runner with an owner; only those share a list")
    if runner.logins.filter(kind=RunnerLogin.LOGIN).exists():
        raise SetRefused(409, f"{runner.name}'s list holds a full login, which cannot be shared (its refresh "
                              "token rotates): remove it first, or use a token from `claude setup-token`")
    with transaction.atomic():
        Runner.objects.select_for_update().filter(pk=runner.pk).first()
        cset = RunnerCredentialSet.objects.create(name=name, workspace_id=runner.workspace_id,
                                                  owner_id=runner.owner_id)
        RunnerLogin.objects.filter(runner=runner).update(runner=None, credential_set=cset)
        runner.credential_set = cset
        runner.save(update_fields=["credential_set"])
        attach_member(cset, runner, by=by)
    harness_runners.touch_list(runner, by)
    return cset


def join(runner: Runner, cset: RunnerCredentialSet, *, by) -> None:
    """`runner` reads `cset`'s list from now on, and its values from the set's vault for
    its tree (made now if it is the set's first runner there; someone then makes its key
    and seals the values on its Credentials tab). Refused for a fleet member (it reads
    its fleet's), a runner of another owner, and one whose own list still has entries —
    a runner on a set reads only the set's, so they would silently stop being used."""
    if runner.fleet_id:
        raise SetRefused(409, f"{runner.name} is a fleet member: it reads its fleet's set")
    if runner.owner_id != cset.owner_id:
        raise SetRefused(422, "a credential set serves only its owner's runners")
    if runner.kind != Runner.CLOUD:
        raise SetRefused(422, f"{runner.name} is not a cloud runner; only those read a set")
    if runner.credential_set_id == cset.pk:
        return
    if runner.logins.exists():
        raise SetRefused(409, f"{runner.name} has credentials of its own: remove them first (a runner on a "
                              "set reads only the set's list)")
    with transaction.atomic():
        Runner.objects.select_for_update().filter(pk=runner.pk).first()
        runner.credential_set = cset
        runner.save(update_fields=["credential_set"])
        attach_member(cset, runner, by=by)
    harness_runners.touch_list(runner, by)


def leave(runner: Runner, *, by) -> None:
    """`runner` reads its own (empty) list again, and the set's vault no longer serves
    it — its copy of that vault's key is handed out to nobody from now on
    (`workspace_vaults.device_may_hold`). A fleet member cannot leave its fleet's set."""
    from apps.agents.models import VaultAttachment  # noqa: PLC0415 — agents imports harness

    if runner.fleet_id:
        raise SetRefused(409, f"{runner.name} is a fleet member: it reads its fleet's set")
    cset = runner.credential_set
    if cset is None:
        return
    with transaction.atomic():
        Runner.objects.select_for_update().filter(pk=runner.pk).first()
        runner.credential_set = None
        runner.save(update_fields=["credential_set"])
        VaultAttachment.objects.filter(runner=runner, vault__in=cset.vaults.all()).delete()
    harness_runners.touch_list(runner, by)


def summary(cset: RunnerCredentialSet) -> dict:
    """What a Credentials tab says about the set a runner reads: who else reads it, and
    each tree's vault with the names it holds a value for."""
    from apps.agents.models import VaultSecret  # noqa: PLC0415 — agents imports harness

    vaults = vaults_of(cset)
    held: dict = {}
    for vault_id, name in VaultSecret.objects.filter(vault__in=vaults).values_list("vault_id", "name"):
        held.setdefault(vault_id, set()).add(name)
    names = [e.vault_secret_name for e in cset.entries.filter(kind__in=harness_runners.VALUE_KINDS)]
    runners = (Runner.objects.filter(credential_set=cset).exclude(status=Runner.RETIRED)
               .order_by("name").values("id", "name", "workspace_id"))
    return {
        "id": cset.pk, "name": cset.name, "workspace": cset.workspace_id,
        "runners": [{"id": r["id"], "name": r["name"], "workspace": r["workspace_id"]} for r in runners],
        "vaults": [{"vault_id": str(v.pk), "name": v.name, "workspace": v.workspace_id,
                    "root": _root(v.workspace_id), "kv": v.kv,
                    "missing": [n for n in names if n not in held.get(v.pk, set())]} for v in vaults],
    }


def owned_by(user) -> list[RunnerCredentialSet]:
    return list(RunnerCredentialSet.objects.filter(owner=user).order_by("name"))
