"""Canopy Vault Phase 0 (canopy-web#1538): harden what exists.

- FIELD_ENCRYPTION_KEY is its own secret, wired into the task definition.
- `manage.py reencrypt_secrets` moves every *_enc column onto the write key.
- Optional KMS envelope (`kms1:`) for AgentCredential values, off by default.
- `/credentials/resolve` can be scoped with `?refs=`, drops the 1Password key for
  a `canopy-web` agent, and logs every ref it hands out.
"""
from __future__ import annotations

import base64
import hashlib
import os
from io import StringIO
from pathlib import Path

import pytest
import yaml
from botocore.stub import Stubber
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentCredential, AgentCredentialRead
from apps.common import kms_envelope
from apps.common.encryption import encrypt_secret
from apps.passkeys.testing import grant_step_up
from apps.harness.models import Runner, RunnerAssignment
from apps.workspaces.models import Workspace, WorkspaceMembership

AWS = Path(__file__).resolve().parent.parent / "deploy" / "aws"
KEY_ARN = "arn:aws:kms:us-east-1:858923557655:key/11111111-2222-3333-4444-555555555555"


# ---- a KMS double with KMS's semantics: the context must match ---------------

class FakeKms:
    def __init__(self):
        self.wrapped: dict[bytes, tuple[bytes, dict]] = {}
        self.calls: list[str] = []

    def generate_data_key(self, *, KeyId, KeySpec, EncryptionContext):
        assert KeySpec == "AES_256"
        self.calls.append("generate_data_key")
        dk = os.urandom(32)
        blob = os.urandom(48)
        self.wrapped[blob] = (dk, dict(EncryptionContext))
        return {"Plaintext": dk, "CiphertextBlob": blob, "KeyId": KeyId}

    def decrypt(self, *, CiphertextBlob, EncryptionContext):
        self.calls.append("decrypt")
        dk, ctx = self.wrapped[CiphertextBlob]
        if ctx != EncryptionContext:
            raise RuntimeError("InvalidCiphertextException")
        return {"Plaintext": dk}


@pytest.fixture
def kms(settings, monkeypatch):
    settings.VAULT_KMS_KEY_ID = KEY_ARN
    fake = FakeKms()
    monkeypatch.setattr(kms_envelope, "_client", lambda: fake)
    return fake


# ---- the envelope ------------------------------------------------------------

def test_envelope_round_trips_against_the_real_kms_api_shapes(settings, monkeypatch):
    """botocore's Stubber validates the request params against the real KMS
    model, so a misnamed argument fails here rather than in production."""
    import boto3

    settings.VAULT_KMS_KEY_ID = KEY_ARN
    client = boto3.client("kms", region_name="us-east-1",
                          aws_access_key_id="x", aws_secret_access_key="y")
    ctx = {"workspace": "connect", "agent": "7", "name": "gog-token"}
    dk, blob = os.urandom(32), b"wrapped-data-key-blob"
    with Stubber(client) as stub:
        stub.add_response("generate_data_key",
                          {"Plaintext": dk, "CiphertextBlob": blob, "KeyId": KEY_ARN},
                          {"KeyId": KEY_ARN, "KeySpec": "AES_256", "EncryptionContext": ctx})
        stub.add_response("decrypt", {"Plaintext": dk, "KeyId": KEY_ARN},
                          {"CiphertextBlob": blob, "EncryptionContext": ctx})
        monkeypatch.setattr(kms_envelope, "_client", lambda: client)
        sealed = kms_envelope.encrypt("s3cret", context=ctx)
        assert sealed.startswith("kms1:") and "s3cret" not in sealed
        assert kms_envelope.decrypt(sealed, expect=ctx) == "s3cret"
        stub.assert_no_pending_responses()


def test_envelope_refuses_a_value_copied_onto_another_credential(kms):
    sealed = kms_envelope.encrypt("v", context={"workspace": "w", "agent": "1", "name": "a"})
    with pytest.raises(kms_envelope.EnvelopeError):
        kms_envelope.decrypt(sealed, expect={"workspace": "w", "agent": "2", "name": "a"})
    with pytest.raises(kms_envelope.EnvelopeError):
        kms_envelope.decrypt(sealed, expect={"workspace": "w", "agent": "1", "name": "b"})
    # A moved agent (new workspace) still reads its own value.
    assert kms_envelope.decrypt(sealed, expect={"workspace": "w2", "agent": "1", "name": "a"}) == "v"


def test_an_edited_envelope_header_fails_authentication(kms):
    sealed = kms_envelope.encrypt("v", context={"workspace": "w", "agent": "1", "name": "a"})
    parts = sealed[len("kms1:"):].split(".")
    forged = kms_envelope._b64e(b'{"agent":"1","name":"a","workspace":"zz"}')
    with pytest.raises(Exception):
        kms_envelope.decrypt("kms1:" + ".".join([forged, *parts[1:]]))


# ---- fixtures for the agent-credential paths ---------------------------------

@pytest.fixture
def fleet(db, client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(
        slug="eva", name="Eva", workspace=ws,
        runtime_secrets=["canopy-pat", "gog-token"],
        runtime_sources={"nova-api-key": {"op": "op://Agent-Eva/nova/credential"}},
        op_vault="Agent-Eva", op_sa_token_enc=encrypt_secret("ops_tok"),
    )
    runner = Runner.objects.create(
        name="box", kind=Runner.CLOUD, owner=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )
    RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
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


# ---- KMS through the real write/read paths ------------------------------------

def test_with_kms_on_a_new_value_is_stored_kms1_and_resolves(fleet, kms):
    assert _put(fleet["client"], {"canopy-pat": "pat-value"}).status_code == 200
    row = AgentCredential.objects.get(agent=fleet["agent"], name="canopy-pat")
    assert row.value_enc.startswith("kms1:")
    assert _resolve(fleet).json()["values"]["canopy-pat"] == "pat-value"


def test_both_formats_read_whichever_is_being_written(fleet, kms, settings):
    AgentCredential.objects.create(agent=fleet["agent"], name="gog-token",
                                   value_enc=encrypt_secret("fernet-value"))
    _put(fleet["client"], {"canopy-pat": "kms-value"})
    settings.VAULT_KMS_KEY_ID = ""  # switched off again: kms1 rows must still open
    values = _resolve(fleet).json()["values"]
    assert values == {"gog-token": "fernet-value", "canopy-pat": "kms-value"}


def test_kms_off_by_default_writes_fernet(fleet, settings):
    settings.VAULT_KMS_KEY_ID = ""
    _put(fleet["client"], {"canopy-pat": "v"})
    assert not AgentCredential.objects.get(name="canopy-pat").value_enc.startswith("kms1:")


# ---- scoped resolve -----------------------------------------------------------

def test_unscoped_resolve_is_unchanged(fleet):
    _put(fleet["client"], {"canopy-pat": "a", "gog-token": "b"})
    body = _resolve(fleet).json()
    assert body["values"] == {"canopy-pat": "a", "gog-token": "b"}
    assert body["op_sa_token"] == "ops_tok" and body["op_vault"] == "Agent-Eva"


def test_refs_returns_only_named_and_declared(fleet):
    _put(fleet["client"], {"canopy-pat": "a", "gog-token": "b", "undeclared": "c",
                           "nova-api-key": "n"})
    body = _resolve(fleet, "?refs=canopy-pat,undeclared,nova-api-key").json()
    # `undeclared` is stored but not in runtime.yaml: never handed out by name.
    # `nova-api-key` is declared through runtime_sources.
    assert body["values"] == {"canopy-pat": "a", "nova-api-key": "n"}
    # Secret extras ride along only when named.
    assert body["op_sa_token"] == "" and body["op_vault"] == "Agent-Eva"


def test_refs_can_name_a_secret_extra(fleet):
    body = _resolve(fleet, "?refs=op_sa_token").json()
    assert body["values"] == {} and body["op_sa_token"] == "ops_tok"


def test_an_empty_refs_is_a_scope_of_nothing(fleet):
    _put(fleet["client"], {"canopy-pat": "a"})
    body = _resolve(fleet, "?refs=").json()
    assert body["values"] == {} and body["op_sa_token"] == ""


def test_a_canopy_web_agent_is_never_handed_its_1password_key(fleet):
    Agent.objects.filter(pk=fleet["agent"].pk).update(credential_source=Agent.CANOPY_WEB)
    assert _resolve(fleet).json()["op_sa_token"] == ""
    assert _resolve(fleet, "?refs=op_sa_token").json()["op_sa_token"] == ""


def test_every_ref_handed_out_is_logged_with_turn_and_runner(fleet):
    _put(fleet["client"], {"canopy-pat": "a", "gog-token": "b"})
    _resolve(fleet, "?refs=canopy-pat", HTTP_X_CANOPY_TURN_ID="turn-123",
             HTTP_X_CANOPY_RUNNER=str(fleet["runner"].pk))
    reads = list(AgentCredentialRead.objects.filter(agent=fleet["agent"]))
    assert [(r.name, r.turn_id, r.runner_id, r.user_id, r.via) for r in reads] == [
        ("canopy-pat", "turn-123", fleet["runner"].pk, fleet["user"].pk, "runner")]


def test_an_unscoped_read_logs_each_value_and_extra(fleet):
    _put(fleet["client"], {"canopy-pat": "a"})
    _resolve(fleet)
    names = set(AgentCredentialRead.objects.values_list("name", flat=True))
    assert names == {"canopy-pat", "op_sa_token"}


def test_a_runner_header_naming_someone_elses_box_is_not_recorded(fleet):
    other = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    theirs = Runner.objects.create(name="theirs", kind=Runner.CLOUD, owner=other,
                                   status=Runner.ONLINE, capabilities={})
    _put(fleet["client"], {"canopy-pat": "a"})
    _resolve(fleet, "?refs=canopy-pat", HTTP_X_CANOPY_RUNNER=str(theirs.pk))
    assert AgentCredentialRead.objects.get(name="canopy-pat").runner_id is None


# ---- reencrypt_secrets ----------------------------------------------------------

def _legacy(secret_key: str, plaintext: str) -> str:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
    return Fernet(key).encrypt(plaintext.encode()).decode()


def test_secret_columns_are_discovered_not_listed():
    from apps.common.management.commands.reencrypt_secrets import secret_columns

    found = {f"{m._meta.label}.{f}" for m, f in secret_columns()}
    assert {
        "agents.AgentCredential.value_enc", "agents.Agent.op_sa_token_enc",
        "agents.AgentDelegation.secret_enc", "workspaces.Workspace.shared_op_sa_token_enc",
        "harness.RunnerLogin.value_enc", "slack.SlackInstallation.bot_token_enc",
        "contacts.HcpClient.webhook_secret_encrypted",
    } <= found


def test_reencrypt_dry_run_then_apply_then_idempotent(fleet, settings):
    settings.FIELD_ENCRYPTION_KEY = ""
    old = _legacy(settings.SECRET_KEY, "legacy-value")
    AgentCredential.objects.create(agent=fleet["agent"], name="canopy-pat", value_enc=old)
    settings.FIELD_ENCRYPTION_KEY = "a-new-field-key"
    settings.FIELD_ENCRYPTION_ACCEPT_LEGACY = True

    out = StringIO()
    call_command("reencrypt_secrets", stdout=out)
    assert "would rewrite" in out.getvalue()
    assert AgentCredential.objects.get(name="canopy-pat").value_enc == old  # dry run wrote nothing

    call_command("reencrypt_secrets", "--apply", stdout=StringIO())
    row = AgentCredential.objects.get(name="canopy-pat")
    assert row.value_enc != old and row.plaintext() == "legacy-value"
    new_key = base64.urlsafe_b64encode(hashlib.sha256(b"a-new-field-key").digest())
    assert Fernet(new_key).decrypt(row.value_enc.encode()) == b"legacy-value"
    # The agent's vault key (another *_enc column) moved too.
    fleet["agent"].refresh_from_db()
    assert Fernet(new_key).decrypt(fleet["agent"].op_sa_token_enc.encode()) == b"ops_tok"

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
    AgentCredential.objects.create(agent=fleet["agent"], name="canopy-pat", value_enc=old)
    _migration_0053().reencrypt(None, None)
    assert AgentCredential.objects.get(name="canopy-pat").value_enc == old


def test_the_data_migration_moves_legacy_rows_onto_the_field_key(fleet, settings):
    settings.FIELD_ENCRYPTION_KEY = ""
    old = _legacy(settings.SECRET_KEY, "legacy-value")
    AgentCredential.objects.create(agent=fleet["agent"], name="canopy-pat", value_enc=old)
    settings.FIELD_ENCRYPTION_KEY = "a-new-field-key"
    settings.FIELD_ENCRYPTION_ACCEPT_LEGACY = True  # how prod read legacy rows when 0053 ran
    _migration_0053().reencrypt(None, None)
    row = AgentCredential.objects.get(name="canopy-pat")
    new_key = base64.urlsafe_b64encode(hashlib.sha256(b"a-new-field-key").digest())
    assert Fernet(new_key).decrypt(row.value_enc.encode()) == b"legacy-value"


def test_reencrypt_moves_agent_credentials_onto_kms_when_on(fleet, kms):
    AgentCredential.objects.create(agent=fleet["agent"], name="gog-token",
                                   value_enc=encrypt_secret("g"))
    call_command("reencrypt_secrets", "--apply", stdout=StringIO())
    row = AgentCredential.objects.get(name="gog-token")
    assert row.value_enc.startswith("kms1:") and row.plaintext() == "g"


def test_reencrypt_leaves_an_unreadable_value_untouched(fleet):
    AgentCredential.objects.create(agent=fleet["agent"], name="canopy-pat",
                                   value_enc=Fernet(Fernet.generate_key()).encrypt(b"x").decode())
    before = AgentCredential.objects.get(name="canopy-pat").value_enc
    err = StringIO()
    call_command("reencrypt_secrets", "--apply", stdout=StringIO(), stderr=err)
    assert AgentCredential.objects.get(name="canopy-pat").value_enc == before
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
