"""A cloud runner's Claude credential list: one ordered list of logins, tokens and API
keys, whose token and API-key VALUES live only in a vault serving the runner (spec
2026-10-23-runner-claude-credentials-in-the-vault-design.md, step 3).

canopy-web keeps the list — names, kinds, order — and never a value: nothing takes a
plaintext token or key, and the bundle the box reads names each value's vault secret
instead. The values themselves (sealed by the box or a browser, opened by the box's own
device key) are tests/test_runner_vault.py's subject.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.credentials import workspace_vaults
from apps.agents.models import Vault, VaultSecret
from apps.harness import runners
from apps.harness.models import Runner, RunnerCredential, RunnerLogin
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

ZK = "zk1:" + "A" * 60


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@dimagi.com", "pw")


@pytest.fixture()
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


@pytest.fixture()
def runner(owner):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    r = Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=owner, workspace=ws)
    v = workspace_vaults.create_for_runner(r, by=owner)
    Vault.objects.filter(pk=v.pk).update(kv=1)
    return r


def _held(runner, kind="api_key", name=""):
    """An entry whose value the runner's vault holds — the only kind there is now."""
    entry = runners.add_runner_credential(runner, kind=kind, name=name)
    VaultSecret.objects.create(vault=runners.open_runner_vault(runner), name=entry.vault_secret_name,
                               value_zk=ZK, kv=1)
    return entry


def _cred_url(runner):
    return f"/api/harness/runners/{runner.id}/credential"


def _list(client, runner):
    return client.get(f"{_cred_url(runner)}/status").json()["credentials"]


# --- no value, anywhere -------------------------------------------------------

def test_the_bundle_names_vault_secrets_and_carries_no_value(client, runner):
    RunnerLogin.objects.create(runner=runner, key="aaaa1111", kind=RunnerLogin.LOGIN, position=0)
    key = _held(runner, name="billing").key
    got = client.get(_cred_url(runner)).json()
    assert [lg["key"] for lg in got["logins"]] == ["aaaa1111"]
    assert got["cascade"] == [
        {"key": "aaaa1111", "kind": "login", "cli": "claude-code", "vault_secret": ""},
        {"key": key, "kind": "api_key", "cli": "claude-code", "vault_secret": f"anthropic-api-key:{key}"},
    ]
    assert got["vault_open"] is True
    assert not ({"claude_token", "claude_token_secondary", "claude_api_key"} & set(got))


def test_no_route_takes_a_plaintext_value(client, runner):
    """The old fixed-slot write (wire.sh's) and the paste route are gone; a rename
    route sent a value refuses it rather than dropping it on the floor."""
    assert client.post(_cred_url(runner), data={"claude_token": "sk-ant-oat01-x"},
                       content_type="application/json").status_code == 405
    pasted = client.post(f"/api/harness/runners/{runner.id}/credentials",
                         data={"kind": "api_key", "value": "sk-ant-api-x"}, content_type="application/json")
    assert pasted.status_code in (404, 405)
    key = _held(runner).key
    patched = client.patch(f"/api/harness/runners/{runner.id}/credentials/{key}",
                           data={"value": "sk-ant-api-new"}, content_type="application/json")
    assert patched.status_code == 422


def test_an_entry_needs_a_vault_with_a_key(owner):
    ws = Workspace.objects.get(slug="dimagi") if Workspace.objects.filter(slug="dimagi").exists() else \
        Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    bare = Runner.objects.create(name="cloud-2", kind=Runner.CLOUD, owner=owner, workspace=ws)
    with pytest.raises(runners.CredentialRefused, match="make its key first"):
        runners.add_runner_credential(bare, kind="token")


def test_a_login_is_not_pasted(runner):
    with pytest.raises(runners.CredentialRefused):
        runners.add_runner_credential(runner, kind="login")


# --- the list: masked, ordered, named --------------------------------------------

def test_status_is_masked_and_does_not_touch_the_audit_trail(client, runner):
    _held(runner, kind="token", name="hal")
    before = RunnerCredential.objects.get(runner=runner).updated_at
    body = client.get(f"{_cred_url(runner)}/status").json()
    [entry] = body["credentials"]
    assert (entry["kind"], entry["name"], entry["has_value"], entry["in_vault"]) == ("token", "hal", True, True)
    assert body["has_claude_token"] is True and body["claude_token_label"] == "hal"
    assert RunnerCredential.objects.get(runner=runner).updated_at == before


def test_status_is_administrator_gated(runner):
    other = User.objects.create_user("other", "other@dimagi.com", "pw")
    c = Client()
    c.force_login(other)
    assert c.get(f"{_cred_url(runner)}/status").status_code == 404


def test_any_number_of_api_keys_and_tokens_in_any_order(client, runner):
    a, b, c = _held(runner, "api_key"), _held(runner, "token"), _held(runner, "api_key")
    r = client.put(f"/api/harness/runners/{runner.id}/credentials/order",
                   data={"keys": [c.key, a.key, b.key]}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert [e["key"] for e in r.json()["credentials"]] == [c.key, a.key, b.key]


def test_an_order_must_name_every_credential(client, runner):
    a, _ = _held(runner), _held(runner, "token")
    r = client.put(f"/api/harness/runners/{runner.id}/credentials/order",
                   data={"keys": [a.key]}, content_type="application/json")
    assert r.status_code == 422


def test_rename_and_remove(client, runner):
    first, second = _held(runner), _held(runner)
    base = f"/api/harness/runners/{runner.id}/credentials"
    r = client.patch(f"{base}/{first.key}", data={"name": "billing"}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert _list(client, runner)[0]["name"] == "billing"
    assert client.delete(f"{base}/{second.key}").status_code == 200
    assert client.delete(f"{base}/{first.key}").status_code == 422, "the last key is kept"
    assert client.delete(f"{base}/nosuch").status_code == 404


def test_changes_to_the_list_move_the_bundle_stamp_and_a_rename_does_not(runner):
    key = _held(runner).key
    before = RunnerCredential.objects.get(runner=runner).updated_at
    runners.update_runner_credential(runner, key, name="billing")
    assert RunnerCredential.objects.get(runner=runner).updated_at == before
    _held(runner, "token")
    assert RunnerCredential.objects.get(runner=runner).updated_at > before


def test_the_old_logins_order_route_reorders_just_the_logins(client, runner):
    RunnerLogin.objects.create(runner=runner, key="aa", kind=RunnerLogin.LOGIN, position=0)
    _held(runner)
    RunnerLogin.objects.create(runner=runner, key="bb", kind=RunnerLogin.LOGIN, position=2)
    r = client.put(f"/api/harness/runners/{runner.id}/logins/order", data={"keys": ["bb", "aa"]},
                   content_type="application/json")
    assert r.status_code == 200, r.content
    assert [c["kind"] if c["kind"] != "login" else c["key"] for c in r.json()["credentials"]] == [
        "bb", "api_key", "aa"]
