"""A cloud box reads its Claude credentials from its vault, with its own device key
(canopy-web spec 2026-10-23-runner-claude-credentials-in-the-vault-design.md).

The bundle (`GET …/credential`) names, per token or API key, the vault secret that
holds its value (`vault_secret`). The box opens those with `canopy vault
runner-credentials` and uses the bundle's own `value` only for what the vault did not
open — so a vault hiccup, or a box not yet granted a copy of the key, never stops it.
"""
from __future__ import annotations

import json
import types

import pytest


@pytest.fixture()
def cr(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_bearer", lambda min_life=0: "box-token")
    return cloud_runner


def _bundle(*entries, updated_at="2026-10-23T05:00:00Z"):
    return {"cascade": list(entries), "updated_at": updated_at, "logins": []}


VAULTED = {"key": "t1", "kind": "token", "value": "stored-copy", "vault_secret": "claude-token:t1"}
PLAIN = {"key": "t2", "kind": "token", "value": "plain-only", "vault_secret": ""}


def _cli(cr, monkeypatch, values=None, *, fail=None, shut=()):
    calls = []

    def run(runner_id):
        calls.append(runner_id)
        if fail:
            raise RuntimeError(fail)
        layers = [{"vault": "box vault", "opened": True}] + [{"vault": v, "opened": False} for v in shut]
        return {"values": dict(values or {}), "vault_layers": layers}

    monkeypatch.setattr(cr, "_run_vault_cli", run)
    return calls


def test_a_vaulted_entry_takes_its_value_from_the_vault(cr, monkeypatch):
    _cli(cr, monkeypatch, {"claude-token:t1": "from-the-vault"})
    out = cr._with_vault_values("rid", _bundle(VAULTED, PLAIN))
    assert [e["value"] for e in out["cascade"]] == ["from-the-vault", "plain-only"]
    assert [c[2] for c in cr._cascade_from(out)] == ["from-the-vault", "plain-only"]
    assert cr._HEALTH["claude.vault"]["status"] == "ok"


def test_no_vaulted_entry_means_no_vault_read(cr, monkeypatch):
    calls = _cli(cr, monkeypatch)
    assert cr._with_vault_values("rid", _bundle(PLAIN))["cascade"][0]["value"] == "plain-only"
    assert calls == [] and "claude.vault" not in cr._HEALTH


def test_a_vault_that_cannot_be_read_falls_back_to_the_stored_copy(cr, monkeypatch):
    _cli(cr, monkeypatch, fail="canopy-web refused the device proof")
    out = cr._with_vault_values("rid", _bundle(VAULTED))
    assert out["cascade"][0]["value"] == "stored-copy"
    assert cr._HEALTH["claude.vault"]["status"] == "warn"
    assert "device proof" in cr._HEALTH["claude.vault"]["detail"]


def test_a_vault_the_box_holds_no_key_to_is_named(cr, monkeypatch):
    _cli(cr, monkeypatch, {}, shut=("box vault",))
    out = cr._with_vault_values("rid", _bundle(VAULTED))
    assert out["cascade"][0]["value"] == "stored-copy"
    assert "grant it on the vault's page" in cr._HEALTH["claude.vault"]["detail"]


def test_the_five_minute_reread_is_not_a_vault_read(cr, monkeypatch):
    calls = _cli(cr, monkeypatch, {"claude-token:t1": "v"})
    cr._with_vault_values("rid", _bundle(VAULTED))
    cr._with_vault_values("rid", _bundle(VAULTED))
    assert len(calls) == 1, "an unchanged bundle reuses what the vault gave"
    cr._with_vault_values("rid", _bundle(VAULTED, updated_at="2026-10-23T06:00:00Z"))
    assert len(calls) == 2, "a changed bundle reads the vault again"
    cr._VAULT_VALUES["at"] -= cr.VAULT_REREAD_SECONDS + 1
    cr._with_vault_values("rid", _bundle(VAULTED, updated_at="2026-10-23T06:00:00Z"))
    assert len(calls) == 3, "and so does an hour passing"


def test_a_failed_reread_keeps_the_last_good_values(cr, monkeypatch):
    _cli(cr, monkeypatch, {"claude-token:t1": "good"})
    cr._with_vault_values("rid", _bundle(VAULTED))
    _cli(cr, monkeypatch, fail="timeout")
    out = cr._with_vault_values("rid", _bundle(VAULTED, updated_at="later"))
    assert out["cascade"][0]["value"] == "good"


def test_the_cli_runs_as_the_box_and_its_output_is_parsed(cr, monkeypatch):
    seen = {}

    def run(argv, env=None, **kw):
        seen.update(argv=argv, env=env)
        return types.SimpleNamespace(returncode=0, stderr="",
                                     stdout="noise\n" + json.dumps({"values": {"a": "b"}, "vault_layers": []}))

    monkeypatch.setattr(cr.subprocess, "run", run)
    assert cr._run_vault_cli("rid")["values"] == {"a": "b"}
    assert seen["argv"][1:] == ["vault", "runner-credentials", "--runner", "rid", "--print-json"]
    assert seen["env"]["CANOPY_TOKEN"] == "box-token", "the box's own runner token, never a PAT"


def test_a_cli_failure_carries_its_own_words(cr, monkeypatch):
    monkeypatch.setattr(cr.subprocess, "run", lambda argv, **kw: types.SimpleNamespace(
        returncode=1, stdout="", stderr="Error: no runner token"))
    with pytest.raises(RuntimeError, match="no runner token"):
        cr._run_vault_cli("rid")
