"""Per-runner credential hub: encrypted store + owner-gated set/fetch.

A cloud runner fetches its own secret bundle (Claude token, GitHub token, 1Password
SA token) with its canopy-pat; the value is encrypted at rest and only the runner's
owner (owner) can set or read it."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.common.encryption import decrypt_secret, encrypt_secret
from apps.harness import runners
from apps.harness.models import Runner, RunnerCredential, RunnerLogin

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
    return Runner.objects.create(name="cloud-1", kind=Runner.CLOUD, owner=owner)


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
        data={"claude_token": "CLAUDE", "claude_api_key": "KEY"},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    masked = resp.json()
    assert masked["has_claude_token"] and masked["has_claude_api_key"]
    # A box holds no GitHub credential any more (AgentDelegation, per turn).
    assert "has_github_token" not in masked
    assert "CLAUDE" not in resp.content.decode()  # POST response never leaks values

    # Stored encrypted, not plaintext.
    token = RunnerLogin.objects.get(runner=runner, kind=RunnerLogin.TOKEN)
    assert token.value_enc and token.value_enc != "CLAUDE"

    # The runner's own fetch returns the real values.
    got = client.get(_cred_url(runner)).json()
    assert {k: got[k] for k in ("claude_token", "claude_token_secondary", "claude_api_key")} == {
        "claude_token": "CLAUDE", "claude_token_secondary": "", "claude_api_key": "KEY"}
    assert [(c["kind"], c["value"]) for c in got["cascade"]] == [("token", "CLAUDE"), ("api_key", "KEY")]


def test_set_is_non_clobbering_per_field(client, runner):
    runners.set_runner_credential(runner, claude_token="C", claude_api_key="K")
    # Update only the Claude token; the other two must survive.
    client.post(_cred_url(runner), data={"claude_token": "C2"}, content_type="application/json")
    # Re-fetch fresh (the fixture instance cached its reverse credential relation).
    v = runners.get_runner_credential(Runner.objects.get(pk=runner.pk))
    assert (v["claude_token"], v["claude_token_secondary"], v["claude_api_key"]) == ("C2", "", "K")


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
    key = RunnerLogin.objects.get(runner=runner, kind=RunnerLogin.API_KEY)
    assert key.value_enc
    assert "sk-ant-secret" not in key.value_enc


# --- reading status must not be a write --------------------------------------
def test_status_endpoint_is_masked_and_does_not_touch_the_audit_trail(client, runner):
    """Reading which slots are set used to require a no-op POST, which WROTE —
    bumping updated_at and destroying the only signal for when a credential was
    last actually rotated."""
    client.post(_cred_url(runner), data={"claude_token": "SUB1"},
                content_type="application/json")
    before = RunnerCredential.objects.get(runner=runner).updated_at

    resp = client.get(_cred_url(runner) + "/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_claude_token"] is True
    assert "SUB1" not in resp.content.decode()          # masked, never values

    assert RunnerCredential.objects.get(runner=runner).updated_at == before


def test_an_all_none_update_neither_creates_nor_stamps(client, runner):
    """The old read-as-write shape, now inert."""
    assert not RunnerCredential.objects.filter(runner=runner).exists()
    resp = client.post(_cred_url(runner), data={}, content_type="application/json")
    assert resp.status_code == 200
    assert not RunnerCredential.objects.filter(runner=runner).exists()

    client.post(_cred_url(runner), data={"claude_token": "C"}, content_type="application/json")
    stamped = RunnerCredential.objects.get(runner=runner).updated_at
    client.post(_cred_url(runner), data={}, content_type="application/json")
    assert RunnerCredential.objects.get(runner=runner).updated_at == stamped


def test_status_is_owner_gated_like_the_rest(runner):
    other = User.objects.create_user("other2", "other2@dimagi.com", "pw")
    c = Client()
    c.force_login(other)
    assert c.get(_cred_url(runner) + "/status").status_code == 404


def test_an_empty_value_removes_that_token_and_the_next_moves_up(client, runner):
    """The old fixed-slot write on a list: an empty `claude_token` removes the
    first token, so the second one becomes the first."""
    runners.set_runner_credential(runner, claude_token="C", claude_token_label="ACE",
                                   claude_token_secondary="S", claude_token_secondary_label="JJ")
    resp = client.post(_cred_url(runner), data={"claude_token": ""},
                       content_type="application/json")
    assert resp.status_code == 200, resp.content
    masked = resp.json()
    assert [(c["kind"], c["name"]) for c in masked["credentials"]] == [("token", "JJ")]
    assert masked["has_claude_token_secondary"] is False
    v = runners.get_runner_credential(Runner.objects.get(pk=runner.pk))
    assert (v["claude_token"], v["claude_token_secondary"]) == ("S", "")


def test_the_last_token_or_api_key_cannot_be_removed(client, runner):
    """A token or API key never needs renewing. cloud-ec2-2 had its tokens
    removed, and when its full sign-in stopped renewing the box stopped
    (2026-10-15). Replacing one is fine; taking the last one away is refused."""
    runners.set_runner_credential(runner, claude_token="C", claude_token_secondary="S")
    both = client.post(_cred_url(runner), data={"claude_token": "", "claude_token_secondary": ""},
                       content_type="application/json")
    assert both.status_code == 422
    ok = client.post(_cred_url(runner), data={"claude_token": ""}, content_type="application/json")
    assert ok.status_code == 200, ok.content
    last = client.post(_cred_url(runner), data={"claude_token": ""},
                       content_type="application/json")
    assert last.status_code == 422
    assert "last token or API key" in last.content.decode()
    v = runners.get_runner_credential(Runner.objects.get(pk=runner.pk))
    assert v["claude_token"] == "S"
    replaced = client.post(_cred_url(runner), data={"claude_token": "S2"},
                           content_type="application/json")
    assert replaced.status_code == 200


# --- the credential list ------------------------------------------------------
def _list(client, runner):
    return client.get(f"/api/harness/runners/{runner.id}/credential/status").json()["credentials"]


def test_any_number_of_api_keys_and_tokens_in_any_order(client, runner):
    base = f"/api/harness/runners/{runner.id}/credentials"
    for kind, value, name in (("api_key", "sk-ant-api-1", "ZDR org"), ("token", "tok-1", "ace"),
                              ("api_key", "sk-ant-api-2", "")):
        r = client.post(base, data={"kind": kind, "value": value, "name": name},
                        content_type="application/json")
        assert r.status_code == 200, r.content
        assert value not in r.content.decode()
    entries = _list(client, runner)
    assert [(e["kind"], e["name"], e["has_value"]) for e in entries] == [
        ("api_key", "ZDR org", True), ("token", "ace", True), ("api_key", "", True)]

    keys = [e["key"] for e in entries]
    r = client.put(f"{base}/order", data={"keys": [keys[1], keys[2], keys[0]]},
                   content_type="application/json")
    assert r.status_code == 200, r.content
    got = client.get(_cred_url(runner)).json()
    assert [c["value"] for c in got["cascade"]] == ["tok-1", "sk-ant-api-2", "sk-ant-api-1"]
    # A box from before the list still gets its first token and first API key.
    assert (got["claude_token"], got["claude_api_key"]) == ("tok-1", "sk-ant-api-2")


def test_an_order_must_name_every_credential(client, runner):
    runners.add_runner_credential(runner, kind="api_key", value="k1")
    runners.add_runner_credential(runner, kind="token", value="t1")
    keys = [e["key"] for e in _list(client, runner)]
    r = client.put(f"/api/harness/runners/{runner.id}/credentials/order",
                   data={"keys": keys[:1]}, content_type="application/json")
    assert r.status_code == 422


def test_rename_and_replace_a_key_and_remove_one(client, runner):
    runners.add_runner_credential(runner, kind="api_key", value="k1")
    runners.add_runner_credential(runner, kind="api_key", value="k2")
    first, second = (e["key"] for e in _list(client, runner))
    base = f"/api/harness/runners/{runner.id}/credentials"
    r = client.patch(f"{base}/{first}", data={"name": "billing", "value": "k1-new"},
                     content_type="application/json")
    assert r.status_code == 200, r.content
    assert client.get(_cred_url(runner)).json()["cascade"][0]["value"] == "k1-new"
    assert _list(client, runner)[0]["name"] == "billing"
    assert client.delete(f"{base}/{second}").status_code == 200
    assert client.delete(f"{base}/{first}").status_code == 422, "the last key is kept"
    assert client.delete(f"{base}/nosuch").status_code == 404


def test_a_login_is_not_pasted(client, runner):
    r = client.post(f"/api/harness/runners/{runner.id}/credentials",
                    data={"kind": "login", "value": "x"}, content_type="application/json")
    assert r.status_code == 422


def test_changes_to_the_list_move_the_bundle_stamp(client, runner):
    """A box proves its credentials again when the stamp moves."""
    runners.add_runner_credential(runner, kind="api_key", value="k1")
    before = RunnerCredential.objects.get(runner=runner).updated_at
    runners.add_runner_credential(runner, kind="token", value="t1")
    assert RunnerCredential.objects.get(runner=runner).updated_at > before


def test_the_box_gets_only_logins_in_its_login_list(client, runner):
    """A box from before the list keeps a directory for each entry in `logins`."""
    RunnerLogin.objects.create(runner=runner, key="aaaa1111", kind=RunnerLogin.LOGIN, position=0)
    runners.add_runner_credential(runner, kind="api_key", value="k1")
    got = client.get(_cred_url(runner)).json()
    assert [lg["key"] for lg in got["logins"]] == ["aaaa1111"]
    assert [(c["key"], c["kind"], c["value"]) for c in got["cascade"]] == [
        ("aaaa1111", "login", ""), (got["cascade"][1]["key"], "api_key", "k1")]


def test_a_legacy_write_that_replaces_while_it_removes_is_allowed(client, runner):
    runners.set_runner_credential(runner, claude_token="C")
    r = client.post(_cred_url(runner), data={"claude_token": "", "claude_api_key": "K"},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    assert [c["kind"] for c in r.json()["credentials"]] == ["api_key"]


def test_a_legacy_write_cannot_clear_the_last_api_key(client, runner):
    runners.set_runner_credential(runner, claude_api_key="K")
    r = client.post(_cred_url(runner), data={"claude_api_key": ""}, content_type="application/json")
    assert r.status_code == 422


def test_the_old_logins_order_route_reorders_just_the_logins(client, runner):
    RunnerLogin.objects.create(runner=runner, key="aa", kind=RunnerLogin.LOGIN, position=0)
    runners.add_runner_credential(runner, kind="api_key", value="k1")
    RunnerLogin.objects.create(runner=runner, key="bb", kind=RunnerLogin.LOGIN, position=2)
    r = client.put(f"/api/harness/runners/{runner.id}/logins/order", data={"keys": ["bb", "aa"]},
                   content_type="application/json")
    assert r.status_code == 200, r.content
    assert [c["kind"] if c["kind"] != "login" else c["key"] for c in r.json()["credentials"]] == [
        "bb", "api_key", "aa"]


def test_a_rename_does_not_move_the_bundle_stamp(client, runner):
    key = runners.add_runner_credential(runner, kind="api_key", value="k1").key
    before = RunnerCredential.objects.get(runner=runner).updated_at
    runners.update_runner_credential(runner, key, name="billing")
    assert RunnerCredential.objects.get(runner=runner).updated_at == before
