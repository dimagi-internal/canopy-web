"""A cloud box reads its Claude credentials from its vault, with its own device key
(canopy-web spec 2026-10-23-runner-claude-credentials-in-the-vault-design.md).

The bundle (`GET …/credential`) names, per token or API key, the vault secret that
holds its value (`vault_secret`) — and holds no value itself. The box opens those with
`canopy vault runner-credentials`; what the vault did not open has no value, and the
box moves on down its list (its own logins keep it running).
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


VAULTED = {"key": "t1", "kind": "token", "vault_secret": "claude-token:t1"}
#: A token signed in before its vault held it: on the list, with nothing to use yet.
EMPTY = {"key": "t2", "kind": "token", "vault_secret": ""}


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
    out = cr._with_vault_values("rid", _bundle(VAULTED, EMPTY))
    assert [e.get("value") for e in out["cascade"]] == ["from-the-vault", None]
    assert [c[2] for c in cr._cascade_from(out)] == ["from-the-vault"], "an empty entry is skipped"
    assert cr._HEALTH["claude.vault"]["status"] == "ok"


def test_no_vaulted_entry_means_no_vault_read(cr, monkeypatch):
    calls = _cli(cr, monkeypatch)
    assert "value" not in cr._with_vault_values("rid", _bundle(EMPTY))["cascade"][0]
    assert calls == [] and "claude.vault" not in cr._HEALTH


def test_a_vault_that_cannot_be_read_says_why_and_the_box_moves_on(cr, monkeypatch):
    _cli(cr, monkeypatch, fail="canopy-web refused the device proof")
    out = cr._with_vault_values("rid", _bundle(VAULTED))
    assert cr._cascade_from(out) == []
    assert cr._HEALTH["claude.vault"]["status"] == "warn"
    assert "device proof" in cr._HEALTH["claude.vault"]["detail"]


def test_a_vault_the_box_holds_no_key_to_is_named(cr, monkeypatch):
    _cli(cr, monkeypatch, {}, shut=("box vault",))
    cr._with_vault_values("rid", _bundle(VAULTED))
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


def test_a_grant_is_picked_up_within_a_minute(cr, monkeypatch):
    # The grant changes nothing in the bundle, so only the clock can send the box back.
    calls = _cli(cr, monkeypatch, {}, shut=("box vault",))
    cr._with_vault_values("rid", _bundle(VAULTED))
    cr._with_vault_values("rid", _bundle(VAULTED))
    assert len(calls) == 1
    calls = _cli(cr, monkeypatch, {"claude-token:t1": "granted"})
    cr._VAULT_VALUES["at"] -= cr.VAULT_SHUT_REREAD_SECONDS + 1
    out = cr._with_vault_values("rid", _bundle(VAULTED))
    assert len(calls) == 1 and out["cascade"][0]["value"] == "granted"
    cr._VAULT_VALUES["at"] -= cr.VAULT_SHUT_REREAD_SECONDS + 1
    cr._with_vault_values("rid", _bundle(VAULTED))
    assert len(calls) == 1, "an opened vault goes back to the hour"


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


# ── sealing a sign-in: the order is test_claude_mint.py's; here, what each step says ──

def _mint_api(cr, monkeypatch, *, entry_status=200):
    posts = []

    def api(method, path, body=None, **kw):
        posts.append((method, path, body))
        return (entry_status if path.endswith("/mint/entry") else 200), {"detail": "not completing"}

    monkeypatch.setattr(cr, "_api", api)
    monkeypatch.setattr(cr, "_reload_claude_credentials", lambda: False)
    return posts


def test_a_sign_in_for_a_token_seals_under_that_tokens_key(cr, monkeypatch):
    posts = _mint_api(cr, monkeypatch)
    sealed = []
    monkeypatch.setattr(cr, "_run_seal_cli", lambda rid, values: sealed.append(values) or {"names": [], "vault_id": "v"})
    cr._VAULT_STATE["open"] = True
    cr._seal_minted("rid", {"login_key": "a62534be"}, "tok", "m1234567")
    assert posts[0] == ("POST", "/runners/rid/mint/entry", {"vault_key": "a62534be"})
    assert sealed == [{"claude-token:a62534be": "tok"}]
    assert posts[-1] == ("POST", "/runners/rid/mint/result", {"vault_key": "a62534be"})


def test_an_entry_canopy_web_refuses_is_a_failed_sign_in(cr, monkeypatch):
    posts = _mint_api(cr, monkeypatch, entry_status=422)
    monkeypatch.setattr(cr, "_run_seal_cli", lambda *a: (_ for _ in ()).throw(AssertionError("must not seal")))
    cr._VAULT_STATE["open"] = True
    cr._seal_minted("rid", {"login_key": ""}, "sk-ant-oat01-SECRET", "m1234567")
    assert posts[-1][1] == "/runners/rid/mint/result"
    assert "would not name an entry" in posts[-1][2]["detail"]
    assert "SECRET" not in json.dumps(posts)


def test_the_seal_cli_takes_values_on_stdin_not_argv(cr, monkeypatch):
    seen = {}

    def run(argv, input=None, env=None, **kw):
        seen.update(argv=argv, input=input, env=env)
        return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"names": ["n"], "vault_id": "v"}))

    monkeypatch.setattr(cr.subprocess, "run", run)
    cr._run_seal_cli("rid", {"n": "secret-value"})
    assert "secret-value" not in " ".join(seen["argv"])
    assert json.loads(seen["input"]) == {"n": "secret-value"}
    assert seen["env"]["CANOPY_TOKEN"] == "box-token"
