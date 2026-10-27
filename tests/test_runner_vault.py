"""A cloud runner's Claude credentials live in a vault it opens with its own device key
(spec 2026-10-23-runner-claude-credentials-in-the-vault-design.md).

The vault is the real one — a workspace vault, zero-knowledge, with first-class
membership — and the one extension is that it can serve a RUNNER. A box's device key
may hold a copy of the vault key only while its owner is a member and the box sits in
one isolation boundary with every other box the vault serves; it reads the ciphertext
through a route only its own runner token, with a device proof, can reach.
"""
from __future__ import annotations

import time

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.credentials import workspace_vaults
from apps.agents.models import Vault, VaultAttachment, VaultKeyWrap, VaultSecret
from apps.harness.models import Runner, RunnerCredentialRead, RunnerDeviceKey, RunnerLogin
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.aws_proof import _active_role, _pat, _proof

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("fresh_cache")]

SESSIONS_HTU = "http://testserver/api/runner-sessions/"


def _resolve_path(runner) -> str:
    return f"/api/harness/runners/{runner.pk}/credential/resolve"


def _runner_token(runner) -> str:
    res = Client().post("/api/runner-sessions/", data={"runner": str(runner.pk)},
                        content_type="application/json", HTTP_X_CANOPY_AWS_PROOF=_proof(SESSIONS_HTU))
    assert res.status_code == 201, res.content
    return res.json()["access_token"]


def _ecdh(key: RunnerDeviceKey) -> RunnerDeviceKey:
    """Approve an ECDH key on `key`, which is what makes a device eligible for a wrap."""
    RunnerDeviceKey.objects.filter(pk=key.pk).update(
        ecdh_jkt=f"ecdh-{key.pk}", ecdh_public_jwk={"kty": "EC", "crv": "P-256", "x": "x", "y": "y"},
        ecdh_approved_at=timezone.now())
    key.refresh_from_db()
    return key


def _box_key(fleet, sts) -> RunnerDeviceKey:
    _active_role(fleet, sts)
    return _ecdh(RunnerDeviceKey.objects.get(runner=fleet["runner"]))


def _token_entry(runner, key="t1", name="hal") -> RunnerLogin:
    return RunnerLogin.objects.create(runner=runner, kind=RunnerLogin.TOKEN, key=key, name=name, position=0)


def _keyed(vault: Vault, device_key=None) -> Vault:
    """A vault with a key (kv 1), and a copy of it for `device_key`."""
    Vault.objects.filter(pk=vault.pk).update(kv=1)
    vault.refresh_from_db()
    if device_key is not None:
        VaultKeyWrap.objects.create(vault=vault, kv=1, kind="device", device_key=device_key,
                                    wrap={"alg": "ecdh-es", "epk": "e", "n": "n", "ct": "c"})
    return vault


def _other_runner(fleet, *, name="ec2-box-2", owner=None, capabilities=None) -> Runner:
    return Runner.objects.create(name=name, kind=Runner.CLOUD, owner=owner or fleet["user"],
                                 workspace=fleet["ws"], status=Runner.ONLINE,
                                 last_heartbeat_at=timezone.now(), capabilities=capabilities or {})


# ── a box's own vault ─────────────────────────────────────────────────────────

def test_a_runner_gets_its_own_vault_attached_and_managed_by_its_owner(fleet):
    res = fleet["client"].post(f"/api/harness/runners/{fleet['runner'].pk}/vault")
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["name"] == "ec2-box Claude credentials" and body["kv"] == 0
    v = Vault.objects.get(pk=body["vault_id"])
    assert v.kind == Vault.WORKSPACE and v.workspace_id == fleet["ws"].slug
    assert VaultAttachment.objects.filter(vault=v, runner=fleet["runner"]).exists()
    from apps.agents.credentials import vault_members
    assert vault_members.is_admin(vault_members.role_of(v, fleet["user"]))
    # Asking again returns the same vault, never a second one.
    again = fleet["client"].post(f"/api/harness/runners/{fleet['runner'].pk}/vault")
    assert again.json()["vault_id"] == body["vault_id"]


def test_only_a_cloud_runner_keeps_its_credentials_in_a_vault(fleet):
    laptop = Runner.objects.create(name="laptop", kind=Runner.EMDASH, owner=fleet["user"],
                                   workspace=fleet["ws"], status=Runner.ONLINE,
                                   last_heartbeat_at=timezone.now(), capabilities={})
    res = fleet["client"].post(f"/api/harness/runners/{laptop.pk}/vault")
    assert res.status_code == 422
    assert res.json()["error"] == "vault_bad_attachment"


# ── sharing stops at the isolation boundary ───────────────────────────────────

def test_two_boxes_in_one_boundary_share_a_vault(fleet):
    v = workspace_vaults.create_for_runner(fleet["runner"], by=fleet["user"])
    second = _other_runner(fleet)
    res = fleet["client"].post(f"/api/vaults/{v.pk}/attachments", data={"runner": str(second.pk)},
                               content_type="application/json")
    assert res.status_code == 200, res.content
    assert {a["runner_name"] for a in res.json()["attachments"]} == {"ec2-box", "ec2-box-2"}
    assert {r.pk for r in workspace_vaults.serving_runners(v)} == {fleet["runner"].pk, second.pk}


@pytest.mark.parametrize("difference", ["owner", "sensitive", "agent_runner"])
def test_a_box_across_the_boundary_is_refused(fleet, difference):
    v = workspace_vaults.create_for_runner(fleet["runner"], by=fleet["user"])
    if difference == "owner":
        sam = get_user_model().objects.create_user(username="sam", email="sam@dimagi.com")
        WorkspaceMembership.objects.create(workspace=fleet["ws"], user=sam, role=WorkspaceMembership.OWNER)
        other = _other_runner(fleet, owner=sam)
    elif difference == "sensitive":
        other = _other_runner(fleet, capabilities={"data_mode": "sensitive_data"})
    else:
        other = _other_runner(fleet, capabilities={"serves": {"kind": "agent", "slug": "eva"}})
    with pytest.raises(workspace_vaults.ZkError) as exc:
        workspace_vaults.attach(v, runner=other, by=fleet["user"])
    assert exc.value.code == "vault_crosses_isolation"


def test_a_box_whose_boundary_changes_is_no_longer_served(fleet, sts):
    key = _box_key(fleet, sts)
    v = workspace_vaults.create_for_runner(fleet["runner"], by=fleet["user"])
    second = _other_runner(fleet)
    workspace_vaults.attach(v, runner=second, by=fleet["user"])
    second_key = _ecdh(RunnerDeviceKey.objects.create(
        runner=second, kind=RunnerDeviceKey.SECURE_ENCLAVE, public_jwk={"kty": "EC"}, jkt="second",
        status=RunnerDeviceKey.ACTIVE))
    _keyed(v, key)
    VaultKeyWrap.objects.create(vault=v, kv=1, kind="device", device_key=second_key, wrap={"alg": "ecdh-es"})
    assert workspace_vaults.device_may_hold(v, second_key)

    Runner.objects.filter(pk=second.pk).update(capabilities={"data_mode": "sensitive_data"})
    second.refresh_from_db()

    assert not workspace_vaults.device_may_hold(v, second_key)
    assert workspace_vaults.runner_vaults(second) == [], "its copy is handed out to nobody"
    assert workspace_vaults.runner_layers_payload(second, second_key, None) == []
    assert workspace_vaults.device_may_hold(v, key), "the first box is still served"


# ── the machine layer never grants on its own ─────────────────────────────────

def test_a_box_is_eligible_only_while_its_owner_is_a_member(fleet, sts):
    key = _box_key(fleet, sts)
    _token_entry(fleet["runner"])
    v = workspace_vaults.create_for_runner(fleet["runner"], by=fleet["user"])
    assert [k.pk for k in workspace_vaults.eligible_device_keys(v)] == [key.pk]
    assert any(t["target"] == str(key.pk) for t in workspace_vaults.pending_targets(_keyed(v)))
    VaultKeyWrap.objects.create(vault=v, kv=1, kind="device", device_key=key, wrap={"alg": "ecdh-es"})
    VaultSecret.objects.create(vault=v, name="claude-token:t1", value_zk="zk1:aaa", kv=1)
    assert workspace_vaults.runner_layers_payload(fleet["runner"], key, None)[0]["wrap"] is not None

    # The owner stops being a member (no longer a workspace owner, direct membership gone).
    WorkspaceMembership.objects.filter(user=fleet["user"]).update(role=WorkspaceMembership.EDITOR)
    from apps.agents.models import VaultMember
    VaultMember.objects.filter(vault=v).delete()

    assert workspace_vaults.eligible_device_keys(v) == []
    assert not workspace_vaults.device_may_hold(v, key)
    [layer] = workspace_vaults.runner_layers_payload(fleet["runner"], key, None)
    assert layer["wrap"] is None and layer["values"] == {}, "the copy it still holds is handed out to nobody"


def test_attaching_a_runner_needs_its_administrator(fleet):
    v = workspace_vaults.create_for_runner(fleet["runner"], by=fleet["user"])
    sam = get_user_model().objects.create_user(username="sam", email="sam@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet["ws"], user=sam, role=WorkspaceMembership.OWNER)
    stranger = _other_runner(fleet, owner=sam, name="sams-box")
    res = fleet["client"].post(f"/api/vaults/{v.pk}/attachments", data={"runner": str(stranger.pk)},
                               content_type="application/json")
    assert res.status_code == 404


# ── the box reads it, and nobody else does ────────────────────────────────────

def _held(fleet, sts):
    key = _box_key(fleet, sts)
    _token_entry(fleet["runner"])
    v = _keyed(workspace_vaults.create_for_runner(fleet["runner"], by=fleet["user"]), key)
    VaultSecret.objects.create(vault=v, name="claude-token:t1", value_zk="zk1:aaa", kv=1)
    VaultSecret.objects.create(vault=v, name="something-else", value_zk="zk1:bbb", kv=1)
    return key, v


def test_the_box_reads_its_vault_with_its_own_token_and_proof(fleet, sts):
    key, v = _held(fleet, sts)
    raw = _runner_token(fleet["runner"])
    path = _resolve_path(fleet["runner"])
    res = Client().get(path, HTTP_AUTHORIZATION=f"Bearer {raw}",
                       HTTP_X_CANOPY_AWS_PROOF=_proof(f"http://testserver{path}"))
    assert res.status_code == 200, res.content
    [layer] = res.json()["vaults"]
    assert layer["binding_id"] == f"vault:{v.pk}" and layer["kv"] == 1
    assert layer["wrap"]["alg"] == "ecdh-es"
    # Only what the credential list names: never the vault's other secrets.
    assert layer["values"] == {"claude-token:t1": "zk1:aaa"}
    read = RunnerCredentialRead.objects.get()
    assert (read.name, read.device_key_id, read.vault_id) == ("claude-token:t1", key.pk, v.pk)


def test_a_persons_token_or_session_never_reads_it(fleet, sts):
    _held(fleet, sts)
    path = _resolve_path(fleet["runner"])
    pat = Client().get(path, HTTP_AUTHORIZATION=f"Bearer {_pat(fleet['user'])}",
                       HTTP_X_CANOPY_AWS_PROOF=_proof(f"http://testserver{path}"))
    assert pat.status_code == 403 and pat.json()["error"] == "runner_only"
    browser = Client()
    browser.force_login(fleet["user"])
    grant_step_up(browser)
    assert browser.get(path).status_code == 403
    assert not RunnerCredentialRead.objects.exists()


def test_the_box_needs_a_device_proof(fleet, sts):
    _held(fleet, sts)
    raw = _runner_token(fleet["runner"])
    res = Client().get(_resolve_path(fleet["runner"]), HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert res.status_code == 401 and res.json()["error"] == "device_proof_required"


# ── the bundle says where each value lives ────────────────────────────────────

def test_the_bundle_points_the_box_at_the_vault(fleet, sts):
    _held(fleet, sts)
    _token_entry(fleet["runner"], key="t2", name="not-yet-moved")
    bundle = fleet["client"].get(f"/api/harness/runners/{fleet['runner'].pk}/credential").json()
    by_key = {c["key"]: c for c in bundle["cascade"]}
    assert by_key["t1"]["vault_secret"] == "claude-token:t1"
    assert by_key["t2"]["vault_secret"] == ""

    status = fleet["client"].get(f"/api/harness/runners/{fleet['runner'].pk}/credential/status").json()
    entries = {c["key"]: c for c in status["credentials"]}
    assert entries["t1"]["in_vault"] is True and entries["t1"]["has_value"] is True
    assert entries["t2"]["in_vault"] is False
    assert [v["name"] for v in status["vaults"]] == ["ec2-box Claude credentials"]


# ── step 2: values go INTO the vault, never through canopy-web ────────────────

ZK = "zk1:" + "A" * 60   # shape-valid ciphertext; canopy-web never opens it


def _values_path(runner) -> str:
    return f"/api/harness/runners/{runner.pk}/vault/values"


def _box_put(fleet, body, *, token=None, iat=None):
    """`iat`: a proof is single-use, so a second write in the same second needs its own."""
    path = _values_path(fleet["runner"])
    return Client().put(path, data=body, content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {token or _runner_token(fleet['runner'])}",
                        HTTP_X_CANOPY_AWS_PROOF=_proof(f"http://testserver{path}", iat=iat))


def _open_vault(fleet, sts):
    key = _box_key(fleet, sts)
    v = _keyed(workspace_vaults.create_for_runner(fleet["runner"], by=fleet["user"]), key)
    return key, v


def test_the_box_seals_a_value_into_its_vault_and_the_bundle_moves(fleet, sts):
    from apps.harness.models import RunnerCredential
    _, v = _open_vault(fleet, sts)
    _token_entry(fleet["runner"])
    before = RunnerCredential.objects.get_or_create(runner=fleet["runner"])[0].updated_at

    res = _box_put(fleet, {"vault_id": str(v.pk), "kv": 1, "values": [{"name": "claude-token:t1", "value_zk": ZK}]})
    assert res.status_code == 200, res.content
    assert res.json()["names"] == ["claude-token:t1"]
    assert VaultSecret.objects.get(vault=v, name="claude-token:t1").value_zk == ZK
    assert RunnerCredential.objects.get(runner=fleet["runner"]).updated_at > before, "the box re-reads"


def test_the_box_writes_only_names_on_its_own_list(fleet, sts):
    _, v = _open_vault(fleet, sts)
    _token_entry(fleet["runner"])
    raw = _runner_token(fleet["runner"])
    res = _box_put(fleet, {"vault_id": str(v.pk), "kv": 1, "values": [{"name": "gog-token", "value_zk": ZK}]},
                   token=raw)
    assert res.status_code == 422
    stale = _box_put(fleet, {"vault_id": str(v.pk), "kv": 2, "values": [{"name": "claude-token:t1", "value_zk": ZK}]},
                     token=raw, iat=time.time() - 5)
    assert stale.status_code == 409 and stale.json()["error"] == "zk_kv_mismatch"


def test_no_one_but_the_box_writes_its_vault_values(fleet, sts):
    _, v = _open_vault(fleet, sts)
    _token_entry(fleet["runner"])
    res = _box_put(fleet, {"vault_id": str(v.pk), "kv": 1,
                           "values": [{"name": "claude-token:t1", "value_zk": ZK}]}, token=_pat(fleet["user"]))
    assert res.status_code == 403 and res.json()["error"] == "runner_only"
    assert not VaultSecret.objects.filter(vault=v).exists()


def test_a_pasted_key_is_an_entry_with_no_value_here(fleet, sts):
    path = f"/api/harness/runners/{fleet['runner'].pk}/vault/entries"
    workspace_vaults.create_for_runner(fleet["runner"], by=fleet["user"])
    early = fleet["client"].post(path, data={"kind": "api_key", "name": "metered"}, content_type="application/json")
    assert early.status_code == 422, "no vault key yet: nowhere to seal it"

    _open_vault(fleet, sts)
    res = fleet["client"].post(path, data={"kind": "api_key", "name": "metered"}, content_type="application/json")
    assert res.status_code == 200, res.content
    body = res.json()
    entry = RunnerLogin.objects.get(runner=fleet["runner"], key=body["key"])
    assert entry.name == "metered"
    assert body["secret_name"] == f"anthropic-api-key:{entry.key}" and body["kv"] == 1


def _mint(fleet, *, login_key=""):
    from apps.harness.models import RunnerMint
    return RunnerMint.objects.create(runner=fleet["runner"], slot=RunnerMint.TOKEN, login_key=login_key,
                                     status=RunnerMint.COMPLETING, requested_by=fleet["user"])


def test_a_sign_in_names_its_entry_before_it_seals(fleet, sts):
    from apps.harness import runners as harness_runners
    _open_vault(fleet, sts)
    harness_runners.name_minted_entry(_mint(fleet), "0a1b2c3d")
    assert RunnerLogin.objects.get(runner=fleet["runner"], key="0a1b2c3d").kind == RunnerLogin.TOKEN

    # A sign-in FOR an existing token keeps that entry; its value comes by the seal.
    _token_entry(fleet["runner"], key="t1")
    harness_runners.name_minted_entry(_mint(fleet, login_key="t1"), "t1")
    assert RunnerLogin.objects.filter(runner=fleet["runner"], key="t1").count() == 1
    with pytest.raises(harness_runners.CredentialRefused):
        harness_runners.name_minted_entry(_mint(fleet, login_key="t1"), "ffffffff")


def test_removing_an_entry_removes_its_secret_unless_another_box_uses_it(fleet, sts):
    from apps.harness import runners as harness_runners
    _, v = _open_vault(fleet, sts)
    for k in ("t1", "t2", "t3"):
        _token_entry(fleet["runner"], key=k)
        VaultSecret.objects.create(vault=v, name=f"claude-token:{k}", value_zk=ZK, kv=1)
    second = _other_runner(fleet)
    workspace_vaults.attach(v, runner=second, by=fleet["user"])
    RunnerLogin.objects.create(runner=second, kind=RunnerLogin.TOKEN, key="t2", position=0)

    assert harness_runners.remove_runner_credential(fleet["runner"], "t1")
    assert harness_runners.remove_runner_credential(fleet["runner"], "t2")
    names = set(VaultSecret.objects.filter(vault=v).values_list("name", flat=True))
    assert names == {"claude-token:t2", "claude-token:t3"}, "t2 stays: the other box still lists it"


def test_a_token_held_only_in_the_vault_counts_as_a_backup(fleet, sts):
    from apps.harness import runners as harness_runners
    _, v = _open_vault(fleet, sts)
    _token_entry(fleet["runner"], key="t1")
    VaultSecret.objects.create(vault=v, name="claude-token:t1", value_zk=ZK, kv=1)
    with pytest.raises(harness_runners.CredentialRefused):
        harness_runners.remove_runner_credential(fleet["runner"], "t1")


def test_one_runner_reads_like_its_row_in_the_list(fleet):
    res = fleet["client"].get(f"/api/harness/runners/{fleet['runner'].pk}/")
    assert res.status_code == 200, res.content
    body = res.json()
    listed = next(r for r in fleet["client"].get("/api/harness/runners/").json() if r["id"] == body["id"])
    assert body == listed
    assert body["can_administer"] is True


def test_one_runner_the_list_would_not_show_is_a_404(fleet):
    sam = get_user_model().objects.create_user(username="sam", email="sam@dimagi.com")
    other = Client()
    other.force_login(sam)
    assert other.get(f"/api/harness/runners/{fleet['runner'].pk}/").status_code == 404


def test_the_vault_page_says_where_each_runners_key_lives_and_its_access(fleet, sts):
    key, v = _held(fleet, sts)
    RunnerDeviceKey.objects.filter(pk=key.pk).update(ecdh_store="aws-kms")
    waiting = _other_runner(fleet)
    workspace_vaults.attach(v, runner=waiting, by=fleet["user"])
    _ecdh(RunnerDeviceKey.objects.create(runner=waiting, kind=RunnerDeviceKey.SOFTWARE, public_jwk={"kty": "EC"},
                                         jkt="waiting", status=RunnerDeviceKey.ACTIVE, ecdh_store="kms-broker"))
    keyless = _other_runner(fleet, name="ec2-box-3")
    workspace_vaults.attach(v, runner=keyless, by=fleet["user"])
    res = fleet["client"].get(f"/api/vaults/{v.pk}")
    assert res.status_code == 200, res.content
    rows = {a["runner_name"]: (a["access"], a["key_place"]) for a in res.json()["attachments"] if a["runner"]}
    assert rows == {"ec2-box": ("has", "aws-kms"), "ec2-box-2": ("waiting", "kms-broker"), "ec2-box-3": ("none", "")}
