"""Per-runner credential hub: encrypted store + owner-gated set/fetch.

A cloud runner fetches its own secret bundle (Claude token, GitHub token, 1Password
SA token) with its canopy-pat; the value is encrypted at rest and only the runner's
owner (paired_by) can set or read it."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.common.encryption import decrypt_secret, encrypt_secret
from apps.harness import services
from apps.harness.models import Runner, RunnerCredential

pytestmark = pytest.mark.django_db


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
    return Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, paired_by=owner)


def _cred_url(runner):
    return f"/api/harness/runners/{runner.id}/credential"


# --- encryption --------------------------------------------------------------
def test_encryption_roundtrips_and_hides_plaintext():
    tok = "sk-ant-oat01-secretvalue"
    ct = encrypt_secret(tok)
    assert ct and ct != tok  # stored form is ciphertext
    assert decrypt_secret(ct) == tok
    assert encrypt_secret("") == "" and decrypt_secret("") == ""


# --- set + fetch -------------------------------------------------------------
def test_set_then_runner_fetches_actual_values(client, runner):
    resp = client.post(
        _cred_url(runner),
        data={"claude_token": "CLAUDE", "github_token": "GH", "op_sa_token": "OPS"},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    masked = resp.json()
    assert masked["has_claude_token"] and masked["has_github_token"] and masked["has_op_sa_token"]
    assert "CLAUDE" not in resp.content.decode()  # POST response never leaks values

    # Stored encrypted, not plaintext.
    cred = RunnerCredential.objects.get(runner=runner)
    assert cred.claude_token_enc and cred.claude_token_enc != "CLAUDE"

    # The runner's own fetch returns the real values.
    got = client.get(_cred_url(runner)).json()
    assert got == {"claude_token": "CLAUDE", "github_token": "GH", "op_sa_token": "OPS",
                   "claude_token_secondary": "", "claude_api_key": "",
                   "updated_at": got["updated_at"]}


def test_set_is_non_clobbering_per_field(client, runner):
    services.set_runner_credential(runner, claude_token="C", github_token="G", op_sa_token="O")
    # Update only the Claude token; the other two must survive.
    client.post(_cred_url(runner), data={"claude_token": "C2"}, content_type="application/json")
    # Re-fetch fresh (the fixture instance cached its reverse credential relation).
    v = services.get_runner_credential(Runner.objects.get(pk=runner.pk))
    assert v == {"claude_token": "C2", "github_token": "G", "op_sa_token": "O",
                 "claude_token_secondary": "", "claude_api_key": "",
                 "updated_at": v["updated_at"]}


def test_fetch_when_unset_is_empty(client, runner):
    assert client.get(_cred_url(runner)).json()["claude_token"] == ""


# --- owner gating ------------------------------------------------------------
def test_only_the_owner_can_set_or_fetch(runner):
    other = User.objects.create_user("other", "other@dimagi.com", "pw")
    c = Client()
    c.force_login(other)
    assert c.get(_cred_url(runner)).status_code == 404  # no existence leak
    assert c.post(_cred_url(runner), data={"claude_token": "z"},
                  content_type="application/json").status_code == 404
    assert not RunnerCredential.objects.filter(runner=runner).exists()


# --- the failover cascade (subscription-2 + API key) -------------------------
def test_cascade_slots_round_trip_and_are_non_clobbering(client, runner):
    """The whole point of the extra slots: a box that can survive one
    subscription's weekly cap. Setting one must not wipe the others."""
    resp = client.post(
        _cred_url(runner),
        data={"claude_token": "SUB1", "claude_token_secondary": "SUB2",
              "claude_api_key": "sk-ant-1"},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    masked = resp.json()
    assert masked["has_claude_token"]
    assert masked["has_claude_token_secondary"]
    assert masked["has_claude_api_key"]
    assert "SUB2" not in resp.content.decode() and "sk-ant-1" not in resp.content.decode()

    got = client.get(_cred_url(runner)).json()
    assert got["claude_token"] == "SUB1"
    assert got["claude_token_secondary"] == "SUB2"
    assert got["claude_api_key"] == "sk-ant-1"

    # Rotate ONLY the primary — the fallbacks must survive, or a routine rotation
    # silently leaves the box with nothing to fail over to.
    client.post(_cred_url(runner), data={"claude_token": "SUB1-NEW"},
                content_type="application/json")
    got = client.get(_cred_url(runner)).json()
    assert got["claude_token"] == "SUB1-NEW"
    assert got["claude_token_secondary"] == "SUB2"
    assert got["claude_api_key"] == "sk-ant-1"


def test_api_key_is_encrypted_at_rest_like_the_rest(client, runner):
    client.post(_cred_url(runner), data={"claude_api_key": "sk-ant-secret"},
                content_type="application/json")
    cred = RunnerCredential.objects.get(runner=runner)
    assert cred.claude_api_key_enc
    assert "sk-ant-secret" not in cred.claude_api_key_enc
