"""A credential set is one list that runners in several workspace trees read, with a
vault PER TREE for its values (`apps/harness/credential_sets.py`).

The list (names, kinds, order) is shared; the values are not: a vault never serves two
isolation boundaries, so a runner in another tree reads the set's entries and its own
tree's vault, and an entry with no value there is one it skips until someone seals it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.credentials import workspace_vaults
from apps.agents.models import Vault, VaultAttachment, VaultSecret
from apps.harness import credential_sets, fleets
from apps.harness import runners as harness_runners
from apps.harness.models import Runner, RunnerCredentialSet, RunnerLogin
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("fresh_cache")]

SETS = "/api/harness/runner-credential-sets/"
ZK = "zk1:" + "A" * 60   # shape-valid ciphertext; canopy-web never opens it


def _runner(fleet, *, name, ws=None, capabilities=None, owner=None) -> Runner:
    return Runner.objects.create(name=name, kind=Runner.CLOUD, owner=owner or fleet["user"],
                                 workspace=ws or fleet["ws"], status=Runner.ONLINE,
                                 last_heartbeat_at=timezone.now(), capabilities=capabilities or {})


def _family(fleet) -> Workspace:
    """A second workspace TREE of the same person (their family's, say)."""
    ws = Workspace.objects.create(slug="marsh", display_name="Marsh", created_by=fleet["user"])
    WorkspaceMembership.objects.create(workspace=ws, user=fleet["user"], role=WorkspaceMembership.OWNER)
    return ws


def _with_token(runner, key="aaaa0001", name="jj max") -> Vault:
    """`runner`'s own list with one token whose value its own (keyed) vault holds."""
    vault = workspace_vaults.create_for_runner(runner, by=runner.owner)
    Vault.objects.filter(pk=vault.pk).update(kv=1)
    RunnerLogin.objects.create(runner=runner, kind=RunnerLogin.TOKEN, key=key, name=name, position=0)
    VaultSecret.objects.create(vault=vault, name=f"claude-token:{key}", value_zk="zk1:x", kv=1)
    vault.refresh_from_db()
    return vault


def _share(fleet, runner, name="jj credentials"):
    res = fleet["client"].post(SETS, data={"runner": str(runner.pk), "name": name},
                               content_type="application/json")
    assert res.status_code == 201, res.content
    return RunnerCredentialSet.objects.get(pk=res.json()["id"])


def _choose(fleet, runner, cset):
    return fleet["client"].put(f"{SETS}runners/{runner.pk}",
                               data={"credential_set": str(cset.pk) if cset else None},
                               content_type="application/json")


def _status(fleet, runner) -> dict:
    return fleet["client"].get(f"/api/harness/runners/{runner.pk}/credential/status").json()


# ── sharing a list ────────────────────────────────────────────────────────────

def test_sharing_a_list_moves_its_entries_and_vault_and_changes_nothing_it_reads(fleet):
    box = fleet["runner"]
    vault = _with_token(box)
    cset = _share(fleet, box)
    box.refresh_from_db()
    assert box.credential_set_id == cset.pk
    assert list(cset.entries.values_list("key", flat=True)) == ["aaaa0001"]
    assert not RunnerLogin.objects.filter(runner=box).exists()
    assert list(cset.vaults.all()) == [vault]
    status = _status(fleet, box)
    assert [(e["key"], e["has_value"]) for e in status["credentials"]] == [("aaaa0001", True)]
    assert status["credential_set"]["name"] == "jj credentials"
    assert [r["name"] for r in status["credential_set"]["runners"]] == ["ec2-box"]


def test_a_list_holding_a_full_login_is_not_shared(fleet):
    box = fleet["runner"]
    RunnerLogin.objects.create(runner=box, kind=RunnerLogin.LOGIN, key="default", position=0)
    res = fleet["client"].post(SETS, data={"runner": str(box.pk), "name": "x"}, content_type="application/json")
    assert res.status_code == 409 and "full login" in res.json()["detail"]
    assert not RunnerCredentialSet.objects.exists()


# ── joining from the same tree, and from another ──────────────────────────────

def test_a_runner_in_the_same_tree_reads_the_same_vault(fleet):
    vault = _with_token(fleet["runner"])
    cset = _share(fleet, fleet["runner"])
    second = _runner(fleet, name="ec2-box-2")
    assert _choose(fleet, second, cset).status_code == 200
    assert workspace_vaults.runner_vaults(second) == [vault]
    assert [e["has_value"] for e in _status(fleet, second)["credentials"]] == [True]


def test_a_runner_in_another_tree_gets_that_trees_own_vault(fleet):
    dimagi_vault = _with_token(fleet["runner"])
    cset = _share(fleet, fleet["runner"])
    marsh = _runner(fleet, name="cloud-ec2-2-marsh", ws=_family(fleet),
                    capabilities={"unit": {"kind": "tree", "root": "marsh"}})

    res = _choose(fleet, marsh, cset)
    assert res.status_code == 200, res.content
    body = res.json()
    # The same list…
    assert [e["key"] for e in body["credentials"]] == ["aaaa0001"]
    # …but a vault of its own tree, with no value in it yet, which the dimagi one never serves.
    [marsh_vault] = workspace_vaults.runner_vaults(marsh)
    assert marsh_vault != dimagi_vault and marsh_vault.workspace_id == "marsh"
    assert marsh_vault.name == "jj credentials"
    assert [e["has_value"] for e in body["credentials"]] == [False]
    assert marsh.pk not in {r.pk for r in workspace_vaults.serving_runners(dimagi_vault)}
    assert set(cset.vaults.all()) == {dimagi_vault, marsh_vault}
    by_root = {v["root"]: v for v in body["credential_set"]["vaults"]}
    assert by_root["connect"]["missing"] == [] and by_root["marsh"]["missing"] == ["claude-token:aaaa0001"]

    # Its own tree's value, sealed there (by its box after a sign-in, or a browser), is its alone.
    Vault.objects.filter(pk=marsh_vault.pk).update(kv=1)
    marsh.refresh_from_db()
    harness_runners.store_vault_values(marsh, vault_id=str(marsh_vault.pk), kv=1,
                                       values={"claude-token:aaaa0001": ZK})
    assert [e["has_value"] for e in _status(fleet, marsh)["credentials"]] == [True]
    assert VaultSecret.objects.get(vault=dimagi_vault).value_zk == "zk1:x"

    # A second runner of that tree joins the tree's vault, not a third one.
    marsh2 = _runner(fleet, name="cloud-ec2-3-marsh", ws=marsh.workspace,
                     capabilities={"unit": {"kind": "tree", "root": "marsh"}})
    assert _choose(fleet, marsh2, cset).status_code == 200
    assert workspace_vaults.runner_vaults(marsh2) == [marsh_vault]
    assert cset.vaults.count() == 2


def test_an_entry_removed_from_the_set_leaves_every_trees_vault(fleet):
    dimagi_vault = _with_token(fleet["runner"])
    cset = _share(fleet, fleet["runner"])
    marsh = _runner(fleet, name="cloud-ec2-2-marsh", ws=_family(fleet))
    _choose(fleet, marsh, cset)
    [marsh_vault] = workspace_vaults.runner_vaults(marsh)
    VaultSecret.objects.create(vault=marsh_vault, name="claude-token:aaaa0001", value_zk="zk1:m", kv=1)
    RunnerLogin.objects.create(credential_set=cset, kind=RunnerLogin.API_KEY, key="bbbb0002", position=1)
    for v in (dimagi_vault, marsh_vault):
        VaultSecret.objects.create(vault=v, name="anthropic-api-key:bbbb0002", value_zk="zk1:k", kv=1)

    res = fleet["client"].delete(f"/api/harness/runners/{marsh.pk}/credentials/aaaa0001")
    assert res.status_code == 200, res.content
    assert not VaultSecret.objects.filter(name="claude-token:aaaa0001").exists()
    assert VaultSecret.objects.filter(name="anthropic-api-key:bbbb0002").count() == 2
    assert [e["key"] for e in _status(fleet, fleet["runner"])["credentials"]] == ["bbbb0002"]


def test_a_tree_vault_serves_only_one_isolation_boundary(fleet):
    _with_token(fleet["runner"])
    cset = _share(fleet, fleet["runner"])
    # Same tree, but an agent unit: it may not share the tree's vault.
    unit = _runner(fleet, name="ec2-box-eva", capabilities={"unit": {"kind": "agent", "slug": "eva"}})
    res = _choose(fleet, unit, cset)
    assert res.status_code == 422 and res.json()["error"] == "vault_crosses_isolation"
    unit.refresh_from_db()
    assert unit.credential_set_id is None and not VaultAttachment.objects.filter(runner=unit).exists()


# ── what a join refuses ───────────────────────────────────────────────────────

def test_a_runner_with_credentials_of_its_own_does_not_join(fleet):
    _with_token(fleet["runner"])
    cset = _share(fleet, fleet["runner"])
    other = _runner(fleet, name="ec2-box-2")
    RunnerLogin.objects.create(runner=other, kind=RunnerLogin.API_KEY, key="cccc0003", position=0)
    res = _choose(fleet, other, cset)
    assert res.status_code == 409 and "of its own" in res.json()["detail"]


def test_someone_elses_set_is_not_found(fleet):
    sam = get_user_model().objects.create_user(username="sam", email="sam@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["ws"], user=sam, role=WorkspaceMembership.OWNER)
    theirs = RunnerCredentialSet.objects.create(name="sam's", workspace=fleet["ws"], owner=sam)
    assert _choose(fleet, fleet["runner"], theirs).status_code == 404
    assert fleet["client"].get(SETS).json() == []
    with pytest.raises(credential_sets.SetRefused):
        credential_sets.join(fleet["runner"], theirs, by=fleet["user"])


def test_only_the_runners_owner_puts_it_on_a_set(fleet):
    sam = get_user_model().objects.create_user(username="sam", email="sam@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["ws"], user=sam, role=WorkspaceMembership.OWNER)
    harness_runners.grant_runner_admin(fleet["runner"], fleet["user"])
    samsbox = _runner(fleet, name="sams-box", owner=sam)
    harness_runners.grant_runner_admin(samsbox, fleet["user"])
    cset = RunnerCredentialSet.objects.create(name="jj", workspace=fleet["ws"], owner=fleet["user"])
    assert _choose(fleet, samsbox, cset).status_code == 403


# ── leaving ───────────────────────────────────────────────────────────────────

def test_leaving_a_set_stops_its_vault_serving_the_runner(fleet):
    vault = _with_token(fleet["runner"])
    cset = _share(fleet, fleet["runner"])
    second = _runner(fleet, name="ec2-box-2")
    _choose(fleet, second, cset)
    res = _choose(fleet, second, None)
    assert res.status_code == 200, res.content
    second.refresh_from_db()
    assert second.credential_set_id is None and res.json()["credentials"] == []
    assert workspace_vaults.runner_vaults(second) == []
    assert {r.pk for r in workspace_vaults.serving_runners(vault)} == {fleet["runner"].pk}


# ── a fleet on an existing set ────────────────────────────────────────────────

def test_a_fleet_can_read_an_existing_set(fleet):
    _with_token(fleet["runner"])
    cset = _share(fleet, fleet["runner"])
    invitation = fleets.create(
        user=fleet["user"], name="dimagi", workspace="connect",
        role_arn="arn:aws:iam::123456789012:role/canopy-fleet-dimagi", ecdh_public_jwk=None,
        region="us-east-1", group_name="canopy-fleet-dimagi", min_members=1, max_members=2,
        member_turns=2, credential_set=cset)
    assert invitation.fleet.credential_set_id == cset.pk
    sam = get_user_model().objects.create_user(username="sam", email="sam@dimagi.com")
    with pytest.raises(fleets.FleetRefused):
        fleets.create(user=sam, name="sams", workspace="connect",
                      role_arn="arn:aws:iam::123456789012:role/canopy-fleet-sams", ecdh_public_jwk=None,
                      region="us-east-1", group_name="canopy-fleet-sams", min_members=1, max_members=2,
                      member_turns=2, credential_set=cset)


def test_only_the_sets_owner_changes_a_shared_list(fleet):
    _with_token(fleet["runner"])
    _share(fleet, fleet["runner"])
    sam = get_user_model().objects.create_user(username="sam", email="sam@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["ws"], user=sam, role=WorkspaceMembership.OWNER)
    harness_runners.grant_runner_admin(fleet["runner"], sam)
    client = Client()
    client.force_login(sam)
    grant_step_up(client)
    # An administrator of one runner on the set reads its list, but changes no one else's.
    assert client.get(f"/api/harness/runners/{fleet['runner'].pk}/credential/status").status_code == 200
    res = client.delete(f"/api/harness/runners/{fleet['runner'].pk}/credentials/aaaa0001")
    assert res.status_code == 403 and "only its owner" in res.json()["detail"]
    assert client.post(f"/api/harness/runners/{fleet['runner'].pk}/vault/entries",
                       data={"kind": "api_key"}, content_type="application/json").status_code == 403


# ── what a set shows: its list, and the runners it could be given to ─────────

def test_a_set_names_its_entries_and_the_runners_you_could_add(fleet):
    box = fleet["runner"]
    _with_token(box)
    cset = _share(fleet, box)
    free = _runner(fleet, name="free-box", ws=_family(fleet))
    busy = _runner(fleet, name="busy-box")
    RunnerLogin.objects.create(runner=busy, kind=RunnerLogin.TOKEN, key="bbbb0001", position=0)
    _runner(fleet, name="someone-elses", owner=get_user_model().objects.create_user(username="x", email="x@x.com"))

    listed = fleet["client"].get(SETS).json()[0]
    assert listed["entries"] == [{"name": "jj max", "kind": "token"}]
    by_name = {c["name"]: c for c in listed["candidates"]}
    assert box.name not in by_name and "someone-elses" not in by_name
    assert by_name["free-box"]["refusal"] is None and by_name["free-box"]["new_tree"] is True
    assert by_name["busy-box"]["refusal"].startswith("has credentials of its own")

    assert _choose(fleet, free, cset).status_code == 200
    again = {c["name"] for c in fleet["client"].get(SETS).json()[0]["candidates"]}
    assert "free-box" not in again
