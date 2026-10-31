"""Canopy Vault Phase 0 (canopy-web#1538): harden what exists.

- FIELD_ENCRYPTION_KEY is its own secret, wired into the task definition.
- `manage.py reencrypt_secrets` moves every *_enc column onto the write key.
- `/credentials/resolve` can be scoped with `?refs=` and logs every ref it hands out.
"""
from __future__ import annotations

import base64
import hashlib
from io import StringIO
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.agents import services
from apps.agents.models import Agent, AgentCredentialRead
from apps.connections.models import ConnectionApp
from apps.common.encryption import encrypt_secret
from apps.passkeys.testing import give_vault_key, grant_step_up
from apps.harness.models import Runner, RunnerAssignment
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.device_proof import accept_device_proof

AWS = Path(__file__).resolve().parent.parent / "deploy" / "aws"


# ---- fixtures for the agent-credential paths ---------------------------------

@pytest.fixture
def fleet(db, client, monkeypatch):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(
        slug="eva", name="Eva", workspace=ws,
        runtime_secrets=["canopy-pat", "gog-token"],
        runtime_sources={"nova-api-key": {}},
    )
    runner = Runner.objects.create(
        name="box", kind=Runner.CLOUD, owner=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    give_vault_key(jj)  # someone to seal a value to: every write is zero-knowledge
    # These tests are about scoping and logging; the proof is tests/test_canopy_vault_zk.py.
    accept_device_proof(monkeypatch)
    client.force_login(jj)
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=jj, label="t")
    return {"client": client, "agent": agent, "runner": runner, "user": jj, "pat": raw}


def _put(c, values):
    grant_step_up(c)  # a browser write needs a passkey step-up (tests/test_agent_credential_stepup.py)
    return c.put("/api/agents/eva/credentials", data={"values": values},
                 content_type="application/json")


def _resolve(fleet, qs="", **headers):
    return Client().get(f"/api/agents/eva/credentials/resolve{qs}",
                        HTTP_AUTHORIZATION=f"Bearer {fleet['pat']}", **headers)


# ---- scoped resolve -----------------------------------------------------------

def test_unscoped_resolve_is_unchanged(fleet):
    _put(fleet["client"], {"canopy-pat": "a", "gog-token": "b"})
    body = _resolve(fleet).json()
    # Ciphertext sealed to the agent's readers, never the values.
    assert set(body["values"]) == {"canopy-pat", "gog-token"}
    assert all(v.startswith("zks1:") for v in body["values"].values())
    assert "op_sa_token" not in body and "op_vault" not in body


def test_refs_returns_only_named_and_declared(fleet):
    _put(fleet["client"], {"canopy-pat": "a", "gog-token": "b", "nova-api-key": "n"})
    # Stored without the API (which declares what an admin stores): an orphan.
    services.set_agent_credentials(Agent.objects.get(slug="eva"), {"undeclared": "c"})
    body = _resolve(fleet, "?refs=canopy-pat,undeclared,nova-api-key").json()
    # `undeclared` is stored but not declared: never handed out by name.
    # `nova-api-key` is declared through runtime_sources.
    assert set(body["values"]) == {"canopy-pat", "nova-api-key"}
    assert "op_sa_token" not in body and "op_vault" not in body


def test_naming_the_retired_1password_extra_hands_out_nothing(fleet):
    body = _resolve(fleet, "?refs=op_sa_token").json()
    assert body["values"] == {} and "op_sa_token" not in body


def test_an_empty_refs_is_a_scope_of_nothing(fleet):
    _put(fleet["client"], {"canopy-pat": "a"})
    body = _resolve(fleet, "?refs=").json()
    assert body["values"] == {}


def test_every_ref_handed_out_is_logged_with_turn_and_runner(fleet):
    _put(fleet["client"], {"canopy-pat": "a", "gog-token": "b"})
    _resolve(fleet, "?refs=canopy-pat", HTTP_X_CANOPY_TURN_ID="turn-123",
             HTTP_X_CANOPY_RUNNER=str(fleet["runner"].pk))
    reads = list(AgentCredentialRead.objects.filter(agent=fleet["agent"]))
    assert [(r.name, r.turn_id, r.runner_id, r.user_id, r.via) for r in reads] == [
        ("canopy-pat", "turn-123", fleet["runner"].pk, fleet["user"].pk, "runner")]


def test_an_unscoped_read_logs_each_value(fleet):
    _put(fleet["client"], {"canopy-pat": "a"})
    _resolve(fleet)
    names = set(AgentCredentialRead.objects.values_list("name", flat=True))
    assert names == {"canopy-pat"}


def test_a_runner_header_naming_someone_elses_box_is_not_recorded(fleet):
    other = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    theirs = Runner.objects.create(name="theirs", kind=Runner.CLOUD, owner=other,
                                   status=Runner.ONLINE, capabilities={})
    _put(fleet["client"], {"canopy-pat": "a"})
    _resolve(fleet, "?refs=canopy-pat", HTTP_X_CANOPY_RUNNER=str(theirs.pk))
    assert AgentCredentialRead.objects.get(name="canopy-pat").runner_id != theirs.pk


# ---- reencrypt_secrets ----------------------------------------------------------

def _held(fleet, value: str) -> ConnectionApp:
    """A row with an encrypted column to re-encrypt: a workspace's OAuth client secret."""
    row, _ = ConnectionApp.objects.update_or_create(
        workspace=fleet["agent"].workspace, provider="salesforce", defaults={"client_id": "x", "client_secret_enc": value})
    return row


def _stored(fleet) -> str:
    return ConnectionApp.objects.get(workspace=fleet["agent"].workspace).client_secret_enc


def _legacy(secret_key: str, plaintext: str) -> str:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
    return Fernet(key).encrypt(plaintext.encode()).decode()


def test_secret_columns_are_discovered_not_listed():
    from apps.common.management.commands.reencrypt_secrets import secret_columns

    found = {f"{m._meta.label}.{f}" for m, f in secret_columns()}
    assert {
        "connections.ConnectionApp.client_secret_enc",
        "slack.SlackInstallation.bot_token_enc",
        "contacts.HcpClient.webhook_secret_encrypted",
    } <= found


def test_reencrypt_dry_run_then_apply_then_idempotent(fleet, settings):
    settings.FIELD_ENCRYPTION_KEY = ""
    old = _legacy(settings.SECRET_KEY, "legacy-value")
    _held(fleet, old)
    settings.FIELD_ENCRYPTION_KEY = "a-new-field-key"
    settings.FIELD_ENCRYPTION_ACCEPT_LEGACY = True

    out = StringIO()
    call_command("reencrypt_secrets", stdout=out)
    assert "would rewrite" in out.getvalue()
    assert _stored(fleet) == old  # dry run wrote nothing

    call_command("reencrypt_secrets", "--apply", stdout=StringIO())
    stored = _stored(fleet)
    new_key = base64.urlsafe_b64encode(hashlib.sha256(b"a-new-field-key").digest())
    assert stored != old and Fernet(new_key).decrypt(stored.encode()) == b"legacy-value"

    out = StringIO()
    call_command("reencrypt_secrets", "--apply", stdout=out)
    assert "rewrote 0 value(s)" in out.getvalue()


def test_with_a_field_key_secret_key_no_longer_decrypts(settings):
    from cryptography.fernet import InvalidToken

    from apps.common.encryption import decrypt_secret

    settings.FIELD_ENCRYPTION_KEY = "a-new-field-key"
    old = _legacy(settings.SECRET_KEY, "legacy-value")
    with pytest.raises(InvalidToken):
        decrypt_secret(old)
    settings.FIELD_ENCRYPTION_ACCEPT_LEGACY = True  # the emergency switch
    assert decrypt_secret(old) == "legacy-value"


def test_without_a_field_key_the_legacy_key_still_writes_and_reads(settings):
    from apps.common.encryption import decrypt_secret

    settings.FIELD_ENCRYPTION_KEY = ""
    assert decrypt_secret(_legacy(settings.SECRET_KEY, "dev")) == "dev"
    assert decrypt_secret(encrypt_secret("dev2")) == "dev2"


def test_a_previous_field_key_reads_during_rotation(settings):
    from apps.common.encryption import decrypt_secret

    settings.FIELD_ENCRYPTION_KEY = "old-field-key"
    old = encrypt_secret("v")
    settings.FIELD_ENCRYPTION_KEY = "new-field-key"
    settings.FIELD_ENCRYPTION_PREVIOUS_KEY = "old-field-key"
    assert decrypt_secret(old) == "v"
    new_key = base64.urlsafe_b64encode(hashlib.sha256(b"new-field-key").digest())
    assert Fernet(new_key).decrypt(encrypt_secret("w").encode()) == b"w"


@pytest.mark.django_db
def test_existing_only_matches_the_full_list_on_a_migrated_database():
    from apps.common.management.commands.reencrypt_secrets import secret_columns

    assert secret_columns(existing_only=True) == secret_columns()


def _migration_0053():
    import importlib
    return importlib.import_module("apps.agents.migrations.0053_reencrypt_secrets_under_field_key")


def test_the_data_migration_is_a_noop_without_a_field_key(fleet, settings):
    settings.FIELD_ENCRYPTION_KEY = ""
    old = _legacy(settings.SECRET_KEY, "legacy-value")
    _held(fleet, old)
    _migration_0053().reencrypt(None, None)
    assert _stored(fleet) == old


def test_the_data_migration_moves_legacy_rows_onto_the_field_key(fleet, settings):
    settings.FIELD_ENCRYPTION_KEY = ""
    old = _legacy(settings.SECRET_KEY, "legacy-value")
    _held(fleet, old)
    settings.FIELD_ENCRYPTION_KEY = "a-new-field-key"
    settings.FIELD_ENCRYPTION_ACCEPT_LEGACY = True  # how prod read legacy rows when 0053 ran
    _migration_0053().reencrypt(None, None)
    stored = _stored(fleet)
    new_key = base64.urlsafe_b64encode(hashlib.sha256(b"a-new-field-key").digest())
    assert Fernet(new_key).decrypt(stored.encode()) == b"legacy-value"


def test_reencrypt_leaves_an_unreadable_value_untouched(fleet):
    before = Fernet(Fernet.generate_key()).encrypt(b"x").decode()
    _held(fleet, before)
    err = StringIO()
    call_command("reencrypt_secrets", "--apply", stdout=StringIO(), stderr=err)
    assert _stored(fleet) == before
    assert "left untouched" in err.getvalue()


# ---- infrastructure -------------------------------------------------------------

class _L(yaml.SafeLoader):
    pass


_L.add_multi_constructor("!", lambda loader, suffix, node: {
    suffix: loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
    else loader.construct_sequence(node, deep=True) if isinstance(node, yaml.SequenceNode)
    else loader.construct_mapping(node, deep=True)})


def _template() -> dict:
    return yaml.load((AWS / "canopy-web.cfn.yaml").read_text(), Loader=_L)


def test_field_encryption_key_is_its_own_generated_secret_in_the_task():
    t = _template()
    sec = t["Resources"]["FieldEncryptionKey"]
    assert sec["Type"] == "AWS::SecretsManager::Secret"
    assert sec["Properties"]["Name"] == "canopy-web/field-encryption-key"
    assert "GenerateSecretString" in sec["Properties"]
    container = t["Resources"]["TaskDefinition"]["Properties"]["ContainerDefinitions"][0]
    assert {"Name": "FIELD_ENCRYPTION_KEY", "ValueFrom": {"Ref": "FieldEncryptionKey"}} in container["Secrets"]


def test_vault_kms_is_off_by_default_and_fully_conditional():
    t = _template()
    assert t["Parameters"]["EnableVaultKms"]["Default"] == "false"
    for name in ("VaultKmsKey", "VaultKmsAlias"):
        assert t["Resources"][name]["Condition"] == "VaultKmsEnabled"
    stmts = {s["Sid"]: s for s in t["Resources"]["VaultKmsKey"]["Properties"]["KeyPolicy"]["Statement"]}
    assert stmts["TaskRoleEnvelopeEncryption"]["Principal"] == {"AWS": {"Ref": "TaskRoleArn"}}
    assert set(stmts["TaskRoleEnvelopeEncryption"]["Action"]) == {"kms:GenerateDataKey", "kms:Decrypt"}
    env = t["Resources"]["TaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Environment"]
    conditional = [e for e in env if "If" in e]
    assert conditional and conditional[0]["If"][0] == "VaultKmsEnabled"
    assert conditional[0]["If"][1]["Name"] == "VAULT_KMS_KEY_ID"


def test_deploy_role_carries_the_kms_grants_the_key_needs():
    from deploy.aws import deploy_roles as dr
    from deploy.aws import preflight as pf

    pol = dr.policy_for("canopy-web", dr.APPS["canopy-web"])
    granted = {a for s in pol["Statement"] for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}
    for rtype in ("AWS::KMS::Key", "AWS::KMS::Alias"):
        for action in pf.actions_for(rtype, "Add") + pf.actions_for(rtype, "Modify"):
            assert action in granted, f"{rtype} needs {action}"
    # Only canopy-web gets them.
    other = dr.policy_for("ace-web", dr.APPS["ace-web"])
    assert not any("Kms" in s["Sid"] for s in other["Statement"])
