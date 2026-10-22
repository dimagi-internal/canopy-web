"""Canopy Vault Phase 2 (canopy-web#1550), server half: the zero-knowledge vault.

canopy-web never encrypts or decrypts a ZK value; these tests build every wrap,
seal and value with the NORMATIVE reference (docs/vault/zk-v1/zk_v1_reference.py)
— so a round trip here is the real protocol, end to end, with canopy-web as the
store in the middle — and pin each endpoint's rules.
"""
from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace

import pytest
from canopy_sdk.jose import make_dpop_proof
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.agents import pat_rotation, services, zk, zk_seal
from apps.agents.models import (
    Agent,
    AgentAdmin,
    AgentCredential,
    AgentCredentialAlert,
    AgentCredentialRead,
    AgentPatRotation,
    AgentVaultWrap,
    ZeroKnowledgeValue,
)
from apps.events.models import SettingChange
from apps.harness.models import Runner, RunnerAssignment, RunnerDeviceKey
from apps.passkeys.models import Passkey, UserVaultKeyset
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.vault_zk import VECTORS, ref

pytestmark = pytest.mark.django_db

RESOLVE = "/api/agents/eva/credentials/resolve"
HTU = f"http://testserver{RESOLVE}"
ZK = "/api/agents/eva/vault/zk"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _pat(user) -> str:
    raw, _ = PersonalToken.create_for_user(user=user, label="t")
    return raw


def _jwk(key) -> dict:
    from canopy_sdk.keys import header_jwk

    return header_jwk(key)


# ---- a fleet with one ZK-capable device and one person with a keyset ---------------

class Fleet:
    pass


def _add_passkey(user, cred: str = "cred-1") -> Passkey:
    return Passkey.objects.create(user=user, credential_id=ref.b64u(cred.encode()), public_key=b"x",
                                  prf_enabled=True)


def _make_keyset(user, cred_id: str):
    """A person's keyset exactly as the browser makes one: a key pair, its
    scalar sealed under a (here, fake) PRF output for one passkey."""
    priv = ec.generate_private_key(ec.SECP256R1())
    scalar = priv.private_numbers().private_value.to_bytes(32, "big")
    seal = ref.seal_user_key(scalar, str(user.pk), cred_id, os.urandom(32), os.urandom(16), os.urandom(12))
    ks = UserVaultKeyset.objects.create(user=user, public_jwk=ref.jwk(priv.public_key()), seals=[seal])
    return priv, ks


@pytest.fixture
def fleet(client):
    f = Fleet()
    f.user = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    f.ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=f.user)
    WorkspaceMembership.objects.create(workspace=f.ws, user=f.user, role=WorkspaceMembership.OWNER)
    f.agent = Agent.objects.create(slug="eva", name="Eva", workspace=f.ws, owner=f.user,
                                   runtime_secrets=["CANOPY_WEB_PAT", "kindora"],
                                   credential_source=Agent.CANOPY_WEB)
    f.runner = Runner.objects.create(name="box", kind=Runner.CLOUD, owner=f.user, status=Runner.ONLINE, workspace=f.ws,
                                     last_heartbeat_at=timezone.now(), capabilities={})
    RunnerAssignment.objects.create(agent=f.agent, runner=f.runner, rank=0)
    client.force_login(f.user)
    grant_step_up(client)
    f.client = client
    client.put("/api/agents/eva/credentials", data={"values": {"CANOPY_WEB_PAT": "pat-value",
                                                               "kindora": "kin-value"}},
               content_type="application/json")
    f.pat = _pat(f.user)
    f.sign = ec.generate_private_key(ec.SECP256R1())
    f.ecdh = ec.generate_private_key(ec.SECP256R1())
    f.device = _register(f, f.sign, f.ecdh)
    assert f.client.post(f"/api/runner-device-keys/{f.device.pk}/approve").status_code == 200
    f.device.refresh_from_db()
    f.passkey = _add_passkey(f.user)
    f.member_priv, f.keyset = _make_keyset(f.user, f.passkey.credential_id)
    f.vk = os.urandom(32)
    f.recovery = os.urandom(32)
    return f


def _register(f, sign, ecdh=None, runner=None) -> RunnerDeviceKey:
    body = {"runner": str((runner or f.runner).pk), "public_jwk": _jwk(sign), "kind": "secure-enclave",
            "label": "mbp"}
    if ecdh is not None:
        body["ecdh_public_jwk"] = ref.jwk(ecdh.public_key())
    res = Client().post("/api/runner-device-keys/", data=body, content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {f.pat}")
    assert res.status_code in (200, 201), res.content
    return RunnerDeviceKey.objects.get(pk=res.json()["id"])


def _aid(f) -> str:
    return str(f.agent.pk)


def device_wrap(f, kv=1, vk=None, key=None, ecdh=None) -> dict:
    key, ecdh = key or f.device, ecdh or f.ecdh
    info = ref.device_info(_aid(f), kv, key.ecdh_jkt)
    return {"kind": "device", "target": str(key.pk),
            "wrap": ref.wrap_ecdh_es(vk or f.vk, ecdh.public_key(), info,
                                     ec.generate_private_key(ec.SECP256R1()), os.urandom(12))}


def member_wrap(f, kv=1, vk=None, user=None, pub=None) -> dict:
    user = user or f.user
    pub = pub or f.member_priv.public_key()
    info = ref.member_info(_aid(f), kv, str(user.pk))
    return {"kind": "member", "target": str(user.pk),
            "wrap": ref.wrap_ecdh_es(vk or f.vk, pub, info, ec.generate_private_key(ec.SECP256R1()),
                                     os.urandom(12))}


def recovery_wrap(f, kv=1, vk=None, r=None) -> dict:
    return {"kind": "recovery", "target": None,
            "wrap": ref.wrap_recovery(vk or f.vk, _aid(f), kv, r or f.recovery, os.urandom(16), os.urandom(12))}


def zk_values(f, plain: dict, kv=1, vk=None) -> list[dict]:
    return [{"name": n, "value_zk": ref.encrypt_value(vk or f.vk, _aid(f), n, kv, v, os.urandom(12))}
            for n, v in plain.items()]


PLAIN = {"CANOPY_WEB_PAT": "pat-value", "kindora": "kin-value"}


def _enable(f, *, wraps=None, values=None, kv=1, client=None, **headers):
    body = {"kv": kv,
            "wraps": wraps if wraps is not None else [member_wrap(f), device_wrap(f), recovery_wrap(f)],
            "values": values if values is not None else zk_values(f, PLAIN)}
    return (client or f.client).post(f"{ZK}/enable", data=body, content_type="application/json", **headers)


def _resolve(f, key=None, refs=None):
    url = RESOLVE + (f"?refs={refs}" if refs is not None else "")
    return Client().get(url, HTTP_AUTHORIZATION=f"Bearer {f.pat}",
                        HTTP_DPOP=make_dpop_proof(key or f.sign, "GET", HTU),
                        HTTP_X_CANOPY_TURN_ID="t-1")


# ---- the normative files and the shape checks ----------------------------------------

def test_the_committed_reference_reproduces_the_committed_vectors():
    assert ref.vectors() == VECTORS


def test_shape_checks_accept_every_vector_output():
    out = VECTORS["outputs"]
    zk.check_value_zk(out["value"])
    zk.check_ecdh_wrap(out["device_wrap"])
    zk.check_ecdh_wrap(out["member_wrap"])
    zk.check_recovery_wrap(out["recovery_wrap"])
    zk.check_seal(out["user_key_seal"])


@pytest.mark.parametrize("mutate", [
    lambda w: w | {"alg": "ECDH-ES"},
    lambda w: w | {"n": w["n"] + "AA"},
    lambda w: w | {"ct": w["ct"][:-4]},
    lambda w: w | {"epk": w["epk"][:-2]},
    lambda w: w | {"epk": "Ag" + w["epk"][2:]},  # not an uncompressed point
    lambda w: w | {"n": w["n"] + "="},  # padding
    lambda w: w | {"extra": "x"},
    lambda w: {k: v for k, v in w.items() if k != "ct"},
])
def test_shape_checks_refuse_a_malformed_wrap(mutate):
    with pytest.raises(zk.ZkError) as exc:
        zk.check_ecdh_wrap(mutate(dict(VECTORS["outputs"]["device_wrap"])))
    assert exc.value.code == "zk_invalid_shape"


@pytest.mark.parametrize("value", ["zk2:AAAA", "AAECAwQFBgcICQoL", "zk1:AAECAwQ", "zk1:+/+/" * 10, 42])
def test_shape_checks_refuse_a_malformed_value(value):
    with pytest.raises(zk.ZkError):
        zk.check_value_zk(value)


def test_shape_checks_accept_the_passphrase_seal_and_refuse_a_variant():
    seal = VECTORS["outputs"]["user_key_seal_2skd"]
    zk.check_seal(seal)
    for bad in (seal | {"i": 100000}, seal | {"i": True}, seal | {"kdf": "argon2"},
                seal | {"cred": "x"}, {k: v for k, v in seal.items() if k != "kdf"}):
        with pytest.raises(zk.ZkError):
            zk.check_seal(bad)


def test_a_keyset_may_hold_passkey_and_passphrase_seals(fleet):
    seals = fleet.keyset.seals + [VECTORS["outputs"]["user_key_seal_2skd"]]
    res = fleet.client.put("/api/vault/keyset", data={"public_jwk": fleet.keyset.public_jwk, "seals": seals},
                           content_type="application/json")
    assert res.status_code == 200, res.content
    assert [s["alg"] for s in res.json()["seals"]] == ["prf", "2skd"]
    # Deleting the passkey leaves the passphrase seal.
    fleet.client.delete(f"/api/passkeys/{fleet.passkey.pk}")
    fleet.keyset.refresh_from_db()
    assert [s["alg"] for s in fleet.keyset.seals] == ["2skd"]


def test_shape_checks_refuse_a_malformed_recovery_wrap_or_seal():
    with pytest.raises(zk.ZkError):
        zk.check_recovery_wrap(VECTORS["outputs"]["recovery_wrap"] | {"s": "AAAA"})
    with pytest.raises(zk.ZkError):
        zk.check_seal(VECTORS["outputs"]["user_key_seal"] | {"alg": "ecdh-es"})


# ---- the person's keyset ---------------------------------------------------------------

def test_keyset_get_names_the_prf_input_of_the_reference(client):
    user = get_user_model().objects.create_user(username="p", email="p@dimagi.com")
    client.force_login(user)
    res = client.get("/api/vault/keyset")
    assert res.status_code == 404
    body = res.json()
    assert body["error"] == "no_vault_keyset" and body["exists"] is False and body["user_id"] == str(user.pk)
    assert body["prf_input"] == ref.b64u(ref.prf_input(str(user.pk)))


def test_keyset_put_needs_a_browser_step_up_and_your_own_passkey(client):
    user = get_user_model().objects.create_user(username="p", email="p@dimagi.com")
    pk = _add_passkey(user)
    client.force_login(user)
    priv = ec.generate_private_key(ec.SECP256R1())
    seal = ref.seal_user_key(b"\x01" * 32, str(user.pk), pk.credential_id, os.urandom(32),
                             os.urandom(16), os.urandom(12))
    body = {"public_jwk": ref.jwk(priv.public_key()), "seals": [seal]}
    res = client.put("/api/vault/keyset", data=body, content_type="application/json")
    assert res.status_code == 403 and res.json()["error"] == "step_up_required"
    res = Client().put("/api/vault/keyset", data=body, content_type="application/json",
                       HTTP_AUTHORIZATION=f"Bearer {_pat(user)}")
    assert res.status_code == 403
    grant_step_up(client)
    stranger = dict(seal, cred=ref.b64u(b"someone-elses"))
    res = client.put("/api/vault/keyset", data=body | {"seals": [stranger]}, content_type="application/json")
    assert res.status_code == 422 and res.json()["error"] == "zk_unknown_passkey"
    res = client.put("/api/vault/keyset", data=body, content_type="application/json")
    assert res.status_code == 200, res.content
    got = res.json()
    assert got["exists"] and got["public_jwk"] == ref.jwk(priv.public_key())
    assert got["passkeys"] == [{"credential_id": pk.credential_id, "label": "", "prf_enabled": True,
                                "sealed": True}]


def test_a_new_keyset_needs_replace_and_drops_member_wraps(fleet):
    assert _enable(fleet).status_code == 200
    other = ec.generate_private_key(ec.SECP256R1())
    seal = ref.seal_user_key(b"\x02" * 32, str(fleet.user.pk), fleet.passkey.credential_id,
                             os.urandom(32), os.urandom(16), os.urandom(12))
    body = {"public_jwk": ref.jwk(other.public_key()), "seals": [seal]}
    res = fleet.client.put("/api/vault/keyset", data=body, content_type="application/json")
    assert res.status_code == 409 and res.json()["error"] == "zk_keyset_exists"
    res = fleet.client.put("/api/vault/keyset", data=body | {"replace": True}, content_type="application/json")
    assert res.status_code == 200
    assert not AgentVaultWrap.objects.filter(kind="member").exists()
    pending = fleet.client.get(ZK).json()["pending"]
    assert [(p["kind"], p["target"]) for p in pending] == [("member", str(fleet.user.pk))]


def test_deleting_a_passkey_drops_its_seal(fleet):
    res = fleet.client.delete(f"/api/passkeys/{fleet.passkey.pk}")
    assert res.status_code == 204
    fleet.keyset.refresh_from_db()
    assert fleet.keyset.seals == []


def test_registration_records_prf_support(client, settings):
    from apps.passkeys.testing import SoftAuthenticator

    user = get_user_model().objects.create_user(username="p", email="p@dimagi.com")
    client.force_login(user)
    auth = SoftAuthenticator()
    options = client.post("/api/passkeys/register/begin").json()["options"]
    cred = auth.create(options)
    cred["clientExtensionResults"] = {"prf": {"enabled": True}}
    res = client.post("/api/passkeys/register/finish", data={"credential": cred, "label": "mac"},
                      content_type="application/json")
    assert res.status_code == 201, res.content
    assert res.json()["prf_enabled"] is True
    assert Passkey.objects.get(user=user).prf_enabled is True


# ---- enable ------------------------------------------------------------------------------

def test_enable_wipes_custodial_values_and_turns_zk_on(fleet):
    res = _enable(fleet)
    assert res.status_code == 200, res.content
    state = res.json()
    assert state["enabled"] and state["kv"] == 1 and state["agent_id"] == _aid(fleet)
    assert sorted(w["kind"] for w in state["wraps"]) == ["device", "member", "recovery"]
    assert state["pending"] == [] and state["values"] is None
    dev = next(w for w in state["wraps"] if w["kind"] == "device")
    assert dev["public_jwk"] == ref.jwk(fleet.ecdh.public_key())
    for cred in AgentCredential.objects.filter(agent=fleet.agent):
        assert cred.value_enc == "" and cred.value_zk.startswith("zk1:") and cred.kv == 1
        with pytest.raises(ZeroKnowledgeValue):
            cred.plaintext()
    status = fleet.client.get("/api/agents/eva/credentials/status").json()
    assert {r["source"] for r in status} == {"canopy-web-zk"}
    # The recovery code opens the vault key; the member wrap opens with the keyset.
    rec = AgentVaultWrap.objects.get(kind="recovery").wrap
    code = ref.recovery_code(fleet.recovery)
    assert ref.unwrap_recovery(rec, _aid(fleet), 1, ref.parse_recovery_code(code)) == fleet.vk
    mem = AgentVaultWrap.objects.get(kind="member").wrap
    assert ref.unwrap_ecdh_es(mem, fleet.member_priv, ref.member_info(_aid(fleet), 1, str(fleet.user.pk))) \
        == fleet.vk


def test_enable_is_audited_without_the_ciphertext(fleet):
    assert _enable(fleet).status_code == 200
    row = SettingChange.objects.filter(operation="enable_agent_zk").get()
    assert row.request["body"]["values"] == "[redacted]"


@pytest.mark.parametrize("wraps, code", [
    (lambda f: [member_wrap(f), device_wrap(f)], "zk_recovery_required"),
    (lambda f: [member_wrap(f), recovery_wrap(f), recovery_wrap(f)], "zk_invalid_shape"),
    (lambda f: [member_wrap(f), member_wrap(f), recovery_wrap(f)], "zk_invalid_shape"),
    (lambda f: [member_wrap(f) | {"wrap": {"alg": "ecdh-es"}}, recovery_wrap(f)], "zk_invalid_shape"),
    (lambda f: [member_wrap(f) | {"target": "99999"}, recovery_wrap(f)], "zk_invalid_target"),
    (lambda f: [member_wrap(f), device_wrap(f) | {"target": "not-a-uuid"}, recovery_wrap(f)],
     "zk_invalid_shape"),
])
def test_enable_refuses_a_bad_wrap_set(fleet, wraps, code):
    res = _enable(fleet, wraps=wraps(fleet))
    assert res.status_code == 422 and res.json()["error"] == code, res.content
    assert not Agent.objects.get(pk=fleet.agent.pk).zk_enabled
    assert AgentCredential.objects.get(name="CANOPY_WEB_PAT").plaintext() == "pat-value"


def test_enable_needs_every_stored_value(fleet):
    res = _enable(fleet, values=zk_values(fleet, {"CANOPY_WEB_PAT": "pat-value"}))
    assert res.status_code == 422 and res.json()["error"] == "zk_values_incomplete"
    assert "kindora" in res.json()["detail"]


def test_enable_refuses_kv_other_than_1_and_a_second_enable(fleet):
    assert _enable(fleet, kv=2).json()["error"] == "zk_kv_mismatch"
    assert _enable(fleet).status_code == 200
    res = _enable(fleet)
    assert res.status_code == 409 and res.json()["error"] == "zk_already_enabled"


def test_enable_refuses_a_device_key_that_is_pending_or_has_no_ecdh_key(fleet):
    plain_key = _register(fleet, ec.generate_private_key(ec.SECP256R1()))  # no ECDH key
    fleet.client.post(f"/api/runner-device-keys/{plain_key.pk}/approve")
    pend = _register(fleet, ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1()))
    for key in (plain_key, pend):
        wraps = [member_wrap(fleet), recovery_wrap(fleet),
                 {"kind": "device", "target": str(key.pk), "wrap": device_wrap(fleet)["wrap"]}]
        res = _enable(fleet, wraps=wraps)
        assert res.status_code == 422 and res.json()["error"] == "zk_invalid_target"


def test_enable_from_a_browser_needs_a_step_up_and_a_token_does_not(fleet):
    grant_step_up(fleet.client, seconds=-1)
    res = _enable(fleet)
    assert res.status_code == 403 and res.json()["error"] == "step_up_required"
    res = _enable(fleet, client=Client(), HTTP_AUTHORIZATION=f"Bearer {fleet.pat}")
    assert res.status_code == 200, res.content


def test_enable_by_token_with_only_a_device_wrap_the_caller_owns(fleet):
    # The canopy CLI acts as its device: it may enable without a keyset of its
    # own, as long as a member (here, the owner's keyset) is wrapped too.
    other = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet.ws, user=other, role=WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=fleet.agent, user=other, granted_by=fleet.user)
    UserVaultKeyset.objects.filter(user=fleet.user).delete()
    o_priv, _ = _make_keyset(other, _add_passkey(other, "o-cred").credential_id)
    wraps = [member_wrap(fleet, user=other, pub=o_priv.public_key()), device_wrap(fleet), recovery_wrap(fleet)]
    res = _enable(fleet, wraps=wraps, client=Client(), HTTP_AUTHORIZATION=f"Bearer {fleet.pat}")
    assert res.status_code == 200, res.content


def test_enable_as_a_device_before_anyone_has_a_keyset(fleet):
    # The CLI enables as its device (a runner the caller owns); the owner's
    # member wrap is granted later.
    UserVaultKeyset.objects.all().delete()
    res = _enable(fleet, wraps=[device_wrap(fleet), recovery_wrap(fleet)], client=Client(),
                  HTTP_AUTHORIZATION=f"Bearer {fleet.pat}")
    assert res.status_code == 200, res.content
    body = _resolve(fleet).json()
    assert body["agent_id"] == _aid(fleet)
    state = fleet.client.get(ZK).json()
    dev = state["wraps"][0] if state["wraps"][0]["kind"] == "device" else state["wraps"][1]
    assert dev["ecdh_public_jwk"] == ref.jwk(fleet.ecdh.public_key()) and dev["ecdh_jkt"] == fleet.device.ecdh_jkt


def test_enable_refuses_a_set_the_caller_cannot_open(fleet):
    other = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet.ws, user=other, role=WorkspaceMembership.EDITOR)
    AgentAdmin.objects.create(agent=fleet.agent, user=other, granted_by=fleet.user)
    o_priv, _ = _make_keyset(other, _add_passkey(other, "o-cred").credential_id)
    wraps = [member_wrap(fleet, user=other, pub=o_priv.public_key()), recovery_wrap(fleet)]
    res = _enable(fleet, wraps=wraps)
    assert res.status_code == 422 and res.json()["error"] == "zk_caller_wrap_required"


def test_only_an_admin_reaches_the_zk_routes(fleet):
    editor = get_user_model().objects.create_user(username="e", email="e@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet.ws, user=editor, role=WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(editor)
    grant_step_up(c)
    assert c.get(ZK).status_code == 403
    assert _enable(fleet, client=c).status_code == 403


# ---- after enable: reveal refuses, a plaintext write is sealed --------------------------------

def test_reveal_refuses_and_a_plaintext_write_is_sealed_on_a_zk_agent(fleet):
    assert _enable(fleet).status_code == 200
    res = fleet.client.post("/api/agents/eva/credentials/CANOPY_WEB_PAT/reveal")
    assert res.status_code == 409 and res.json()["error"] == "zk_enabled"
    # Zero-knowledge is the only mode: `canopy vault import` writes in plaintext
    # and canopy-web seals the value to the agent's readers, keeping no copy.
    res = fleet.client.put("/api/agents/eva/credentials", data={"values": {"CANOPY_WEB_PAT": "x"}},
                           content_type="application/json")
    assert res.status_code == 200, res.content
    row = AgentCredential.objects.get(name="CANOPY_WEB_PAT")
    assert row.value_enc == "" and row.value_zk.startswith("zks1:") and row.kv == 1
    assert ref.open_sealed_value(row.value_zk, _aid(fleet), "CANOPY_WEB_PAT", "member", str(fleet.user.pk),
                                 fleet.member_priv) == "x"


# ---- resolve ---------------------------------------------------------------------------------

def test_zk_resolve_hands_out_ciphertext_and_the_devices_wrap(fleet):
    assert _enable(fleet).status_code == 200
    res = _resolve(fleet)
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["zk"] is True and body["kv"] == 1
    vk = ref.unwrap_ecdh_es(body["wrap"], fleet.ecdh,
                            ref.device_info(_aid(fleet), 1, fleet.device.ecdh_jkt))
    assert vk == fleet.vk
    got = {n: ref.decrypt_value(vk, _aid(fleet), n, 1, v) for n, v in body["values"].items()}
    assert got == PLAIN
    assert set(AgentCredentialRead.objects.values_list("name", flat=True)) == set(PLAIN)
    # Scoped like a custodial resolve.
    assert set(_resolve(fleet, refs="kindora").json()["values"]) == {"kindora"}


def test_zk_resolve_needs_a_device_proof_even_without_device_binding(fleet):
    assert _enable(fleet).status_code == 200
    assert not Agent.objects.get(pk=fleet.agent.pk).require_device_binding
    res = Client().get(RESOLVE, HTTP_AUTHORIZATION=f"Bearer {fleet.pat}")
    assert res.status_code == 401 and res.json()["error"] == "device_proof_required"
    assert res.json()["reason"] == "no_valid_proof"


def test_a_device_without_a_wrap_gets_409_and_the_owner_an_alert(fleet):
    assert _enable(fleet, wraps=[member_wrap(fleet), recovery_wrap(fleet)]).status_code == 200
    res = _resolve(fleet)
    assert res.status_code == 409
    body = res.json()
    assert res["content-type"] == "application/problem+json" and body["status"] == 409
    assert {k: body[k] for k in ("error", "kv", "device_key")} == {
        "error": "zk_wrap_missing", "kv": 1, "device_key": str(fleet.device.pk)}
    alert = AgentCredentialAlert.objects.get()
    assert alert.kind == "zk-wrap-missing" and alert.runner_id == fleet.runner.pk
    assert not AgentCredentialRead.objects.exists()


def test_a_custodial_resolve_says_zk_false(fleet):
    body = Client().get(RESOLVE, HTTP_AUTHORIZATION=f"Bearer {fleet.pat}").json()
    assert body["zk"] is False and body["kv"] is None and body["wrap"] is None
    assert body["values"]["CANOPY_WEB_PAT"] == "pat-value"


# ---- values, grants ---------------------------------------------------------------------------

def test_values_put_checks_kv_and_shape(fleet):
    assert _enable(fleet).status_code == 200
    vals = zk_values(fleet, {"new-ref": "nv"})
    res = fleet.client.put(f"{ZK}/values", data={"kv": 2, "values": vals}, content_type="application/json")
    assert res.status_code == 409 and res.json()["error"] == "zk_kv_mismatch"
    res = fleet.client.put(f"{ZK}/values", data={"kv": 1, "values": [{"name": "x", "value_zk": "plain"}]},
                           content_type="application/json")
    assert res.status_code == 422
    res = fleet.client.put(f"{ZK}/values", data={"kv": 1, "values": vals}, content_type="application/json")
    assert res.status_code == 200 and res.json() == {"kv": 1, "names": ["new-ref"]}
    assert AgentCredential.objects.get(name="new-ref").value_zk == vals[0]["value_zk"]


def test_before_enable_every_eligible_principal_is_pending(fleet):
    state = fleet.client.get(ZK).json()
    assert not state["enabled"]
    assert sorted((p["kind"], p["target"]) for p in state["pending"]) == sorted(
        [("device", str(fleet.device.pk)), ("member", str(fleet.user.pk))])


def test_values_put_refuses_an_agent_that_is_not_zk(fleet):
    res = fleet.client.put(f"{ZK}/values", data={"kv": 1, "values": zk_values(fleet, {"a": "b"})},
                           content_type="application/json")
    assert res.status_code == 409 and res.json()["error"] == "zk_not_enabled"


def test_grant_pending_devices_and_admins(fleet):
    assert _enable(fleet, wraps=[member_wrap(fleet), recovery_wrap(fleet)]).status_code == 200
    other = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet.ws, user=other, role=WorkspaceMembership.EDITOR)
    o_priv, _ = _make_keyset(other, _add_passkey(other, "o-cred").credential_id)
    # Not an admin yet: not pending, and not grantable.
    state = fleet.client.get(ZK).json()
    assert [(p["kind"], p["target"]) for p in state["pending"]] == [("device", str(fleet.device.pk))]
    assert state["pending"][0]["public_jwk"] == ref.jwk(fleet.ecdh.public_key())
    assert state["pending"][0]["ecdh_jkt"] == fleet.device.ecdh_jkt
    res = fleet.client.post(f"{ZK}/wraps", data={"kv": 1, "wraps": [
        member_wrap(fleet, user=other, pub=o_priv.public_key())]}, content_type="application/json")
    assert res.status_code == 422 and res.json()["error"] == "zk_invalid_target"
    AgentAdmin.objects.create(agent=fleet.agent, user=other, granted_by=fleet.user)
    assert len(fleet.client.get(ZK).json()["pending"]) == 2
    res = fleet.client.post(f"{ZK}/wraps", data={"kv": 1, "wraps": [
        device_wrap(fleet), member_wrap(fleet, user=other, pub=o_priv.public_key())]},
        content_type="application/json")
    assert res.status_code == 200, res.content
    assert res.json()["pending"] == []
    assert _resolve(fleet).status_code == 200
    # Granting again replaces, never duplicates.
    fleet.client.post(f"{ZK}/wraps", data={"kv": 1, "wraps": [device_wrap(fleet)]},
                      content_type="application/json")
    assert AgentVaultWrap.objects.filter(kind="device").count() == 1


def test_wraps_post_refuses_recovery_and_a_stale_kv(fleet):
    assert _enable(fleet).status_code == 200
    res = fleet.client.post(f"{ZK}/wraps", data={"kv": 1, "wraps": [recovery_wrap(fleet)]},
                            content_type="application/json")
    assert res.status_code == 422
    res = fleet.client.post(f"{ZK}/wraps", data={"kv": 3, "wraps": [device_wrap(fleet)]},
                            content_type="application/json")
    assert res.status_code == 409


def test_state_values_are_ciphertext_and_read_logged(fleet):
    assert _enable(fleet).status_code == 200
    body = fleet.client.get(f"{ZK}?values=true").json()
    assert set(body["values"]) == set(PLAIN) and all(v.startswith("zk1:") for v in body["values"].values())
    assert set(AgentCredentialRead.objects.filter(via="zk-state").values_list("name", flat=True)) == set(PLAIN)


# ---- rotate ------------------------------------------------------------------------------------

def _rotate(f, wraps, values=None, new_kv=2, vk=None):
    vk = vk or os.urandom(32)
    return f.client.post(f"{ZK}/rotate", data={
        "new_kv": new_kv, "wraps": wraps,
        "values": values if values is not None else zk_values(f, PLAIN, kv=new_kv, vk=vk)},
        content_type="application/json")


def test_rotate_replaces_every_wrap_and_value(fleet):
    assert _enable(fleet).status_code == 200
    vk2 = os.urandom(32)
    wraps = [member_wrap(fleet, kv=2, vk=vk2), device_wrap(fleet, kv=2, vk=vk2), recovery_wrap(fleet, kv=2, vk=vk2)]
    res = _rotate(fleet, wraps, values=zk_values(fleet, PLAIN, kv=2, vk=vk2))
    assert res.status_code == 200, res.content
    assert res.json()["kv"] == 2
    assert set(AgentVaultWrap.objects.values_list("kv", flat=True)) == {2}
    assert set(AgentCredential.objects.values_list("kv", flat=True)) == {2}
    body = _resolve(fleet).json()
    vk = ref.unwrap_ecdh_es(body["wrap"], fleet.ecdh, ref.device_info(_aid(fleet), 2, fleet.device.ecdh_jkt))
    assert vk == vk2 and ref.decrypt_value(vk, _aid(fleet), "kindora", 2, body["values"]["kindora"]) == "kin-value"


def test_rotate_needs_the_next_kv_and_every_remaining_target(fleet):
    assert _enable(fleet).status_code == 200
    res = _rotate(fleet, [member_wrap(fleet, kv=3), device_wrap(fleet, kv=3), recovery_wrap(fleet, kv=3)],
                  new_kv=3)
    assert res.status_code == 409 and res.json()["error"] == "zk_kv_mismatch"
    res = _rotate(fleet, [member_wrap(fleet, kv=2), recovery_wrap(fleet, kv=2)])
    assert res.status_code == 422 and res.json()["error"] == "zk_rewrap_required"
    assert str(fleet.device.pk) in res.json()["detail"]
    res = _rotate(fleet, [member_wrap(fleet, kv=2), device_wrap(fleet, kv=2)])
    assert res.json()["error"] == "zk_recovery_required"


def test_rotate_after_revoking_a_device_leaves_it_out(fleet):
    assert _enable(fleet).status_code == 200
    fleet.client.delete(f"/api/runner-device-keys/{fleet.device.pk}")
    state = fleet.client.get(ZK).json()
    assert state["rotation_recommended"] is True
    assert [w["current"] for w in state["wraps"] if w["kind"] == "device"] == [False]
    res = _rotate(fleet, [member_wrap(fleet, kv=2), recovery_wrap(fleet, kv=2)])
    assert res.status_code == 200, res.content
    assert not AgentVaultWrap.objects.filter(kind="device").exists()
    assert res.json()["rotation_recommended"] is False


# ---- device-key enrollment with an ECDH key ------------------------------------------------------

def test_enrollment_records_the_ecdh_key_and_its_thumbprint(fleet):
    assert fleet.device.ecdh_public_jwk == ref.jwk(fleet.ecdh.public_key())
    assert fleet.device.ecdh_jkt == ref.jkt(fleet.ecdh.public_key())
    # The reference's jkt over the vector key matches canopy-web's thumbprint.
    from apps.harness import device_keys

    assert device_keys.thumbprint(VECTORS["inputs"]["device_pub_jwk"]) == VECTORS["inputs"]["device_jkt"]


def test_a_new_ecdh_key_waits_for_approval_but_the_key_stays_active(fleet):
    """A new ECDH key must not cut the box's sign-in (cloud-ec2-2 went offline
    for every agent on 2026-10-18 when it did): only the ECDH key waits."""
    assert _enable(fleet).status_code == 200
    same = _register(fleet, fleet.sign, fleet.ecdh)
    assert same.status == RunnerDeviceKey.ACTIVE and same.ecdh_ready  # re-posting changes nothing
    new_ecdh = ec.generate_private_key(ec.SECP256R1())
    key = _register(fleet, fleet.sign, new_ecdh)
    assert key.pk == fleet.device.pk and key.status == RunnerDeviceKey.ACTIVE
    assert key.ecdh_jkt == ref.jkt(new_ecdh.public_key()) and key.ecdh_waiting and not key.ecdh_ready
    row = Client().get(f"/api/runner-device-keys/?runner={fleet.runner.pk}",
                       HTTP_AUTHORIZATION=f"Bearer {fleet.pat}").json()[0]
    assert row["status"] == "active" and row["ecdh_waiting"] is True and row["reapproval"] is False
    assert row["zk_pending_agents"] == ["eva"]  # approving will also give it access
    assert not AgentVaultWrap.objects.filter(kind="device").exists()
    # Nothing can be wrapped to the unapproved ECDH key.
    from apps.agents import zk

    assert zk.eligible_device_keys(_agent(fleet)) == []
    res = fleet.client.post(f"/api/runner-device-keys/{key.pk}/approve")
    assert res.status_code == 200 and res.json()["zk_pending_agents"] == ["eva"]
    assert res.json()["ecdh_waiting"] is False
    key.refresh_from_db()
    assert key.ecdh_ready and key.ecdh_approved_by_id is not None
    assert [k.pk for k in zk.eligible_device_keys(_agent(fleet))] == [key.pk]


def test_approving_a_new_key_approves_its_ecdh_key_too(fleet):
    key = _register(fleet, ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1()))
    assert key.status == RunnerDeviceKey.PENDING and not key.ecdh_ready and not key.ecdh_waiting
    assert fleet.client.post(f"/api/runner-device-keys/{key.pk}/approve").status_code == 200
    key.refresh_from_db()
    assert key.status == RunnerDeviceKey.ACTIVE and key.ecdh_ready


def test_the_key_list_says_which_machine_what_it_can_open_and_where_its_key_lives(fleet):
    """Settings > Your machines reads "box — AWS server · key held by AWS · can
    open Eva" from these fields."""
    def row():
        return fleet.client.get("/api/runner-device-keys/").json()[0]

    assert row()["can_open"] == []  # not zero-knowledge yet: nothing to open
    assert row()["runner_kind"] == "cloud" and row()["runner_host"] == ""
    assert _enable(fleet).status_code == 200
    assert row()["can_open"] == [{"slug": "eva", "name": "Eva"}]

    def post(ecdh, store):
        body = {"runner": str(fleet.runner.pk), "public_jwk": _jwk(fleet.sign), "kind": "secure-enclave",
                "label": "mbp", "ecdh_public_jwk": ref.jwk(ecdh.public_key()), "ecdh_store": store}
        res = Client().post("/api/runner-device-keys/", data=body, content_type="application/json",
                            HTTP_AUTHORIZATION=f"Bearer {fleet.pat}")
        assert res.status_code == 200, res.content
        return RunnerDeviceKey.objects.get(pk=fleet.device.pk)

    assert post(fleet.ecdh, "secure-enclave").ecdh_store == "secure-enclave"  # said with the same key
    assert post(fleet.ecdh, "bogus").ecdh_store == "secure-enclave"  # an unknown word is ignored
    assert row()["ecdh_store"] == "secure-enclave" and row()["can_open"] == [{"slug": "eva", "name": "Eva"}]
    # A new ECDH key that says nothing of where it lives does not inherit the old label —
    # and, waiting for approval, it can open nothing.
    key = post(ec.generate_private_key(ec.SECP256R1()), "")
    assert key.ecdh_store == "" and key.ecdh_waiting
    assert row()["can_open"] == []


def test_an_aws_iam_key_with_a_new_ecdh_key_stays_active_and_its_ecdh_key_waits(fleet, monkeypatch):
    from apps.harness import aws_iam_proof

    role = "arn:aws:iam::123456789012:role/canopy-runner"
    monkeypatch.setattr(aws_iam_proof, "verify", lambda proof, request: role)

    def enroll(ecdh):
        return Client().post("/api/runner-device-keys/", data={
            "runner": str(fleet.runner.pk), "kind": "aws-iam", "role_arn": role, "proof": "p",
            "ecdh_public_jwk": ref.jwk(ecdh.public_key())},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {fleet.pat}")

    first = enroll(ec.generate_private_key(ec.SECP256R1()))
    assert first.status_code == 201 and first.json()["ecdh_jkt"]
    fleet.client.post(f"/api/runner-device-keys/{first.json()['id']}/approve")
    again = enroll(ec.generate_private_key(ec.SECP256R1()))
    assert again.status_code == 200 and again.json()["status"] == "active"
    assert RunnerDeviceKey.objects.get(pk=first.json()["id"]).ecdh_waiting


def test_enrollment_refuses_a_bad_ecdh_key(fleet):
    res = Client().post("/api/runner-device-keys/", data={
        "runner": str(fleet.runner.pk), "public_jwk": _jwk(ec.generate_private_key(ec.SECP256R1())),
        "kind": "software", "ecdh_public_jwk": {"kty": "OKP", "crv": "X25519", "x": "abc"}},
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {fleet.pat}")
    assert res.status_code == 422


# ==== server-minted values sealed to recipients (zks1, §6), born-ZK (kv 0), the
# settle compare-and-swap, and approve = wrap ==========================================


def _agent(f) -> Agent:
    return Agent.objects.get(pk=f.agent.pk)


def _stored(name: str) -> str:
    return AgentCredential.objects.get(name=name).value_zk


def test_shape_check_accepts_the_vector_and_refuses_variants():
    good = VECTORS["outputs"]["sealed_value"]
    zk_seal.check_sealed_value(good)
    for bad in ("zk1:abc", "zks1:" + ref.b64u(b"not json"), "zks1:" + ref.b64u(b'{"kv":1,"r":[]}')):
        with pytest.raises(zk.ZkError):
            zk_seal.check_sealed_value(bad)


def test_server_seal_opens_with_the_reference_for_device_and_member(fleet):
    assert _enable(fleet).status_code == 200
    services.set_agent_credentials(_agent(fleet), {"gog-token": '{"t":1}'}, minted=True)
    stored = _stored("gog-token")
    assert stored.startswith("zks1:") and AgentCredential.objects.get(name="gog-token").value_enc == ""
    assert ref.open_sealed_value(stored, _aid(fleet), "gog-token", "device", fleet.device.ecdh_jkt,
                                 fleet.ecdh) == '{"t":1}'
    assert ref.open_sealed_value(stored, _aid(fleet), "gog-token", "member", str(fleet.user.pk),
                                 fleet.member_priv) == '{"t":1}'


def test_resolve_hands_a_sealed_value_to_a_device_without_a_wrap(fleet):
    Agent.objects.filter(pk=fleet.agent.pk).update(runtime_secrets=["CANOPY_WEB_PAT", "kindora", "gog-token"])
    assert _enable(fleet, wraps=[member_wrap(fleet), recovery_wrap(fleet)]).status_code == 200
    services.set_agent_credentials(_agent(fleet), {"gog-token": "tok"}, minted=True)
    # Only sealed values asked for: no vault key needed, no 409.
    res = _resolve(fleet, refs="gog-token")
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["wrap"] is None and body["kv"] == 1
    assert ref.open_sealed_value(body["values"]["gog-token"], _aid(fleet), "gog-token", "device",
                                 fleet.device.ecdh_jkt, fleet.ecdh) == "tok"
    # A zk1 value still needs the wrap it lacks.
    assert _resolve(fleet).status_code == 409


def test_state_names_sealed_values_and_a_settle_is_compare_and_swap(fleet):
    assert _enable(fleet).status_code == 200
    services.set_agent_credentials(_agent(fleet), {"CANOPY_WEB_PAT": "minted"}, minted=True)
    state = fleet.client.get(f"{ZK}?values=true").json()
    assert state["sealed"] == ["CANOPY_WEB_PAT"]
    read = state["values"]["CANOPY_WEB_PAT"]
    settled = zk_values(fleet, {"CANOPY_WEB_PAT": "minted"})[0]
    stale = {**settled, "expect": hashlib.sha256(b"something else").hexdigest()}
    res = fleet.client.put(f"{ZK}/values", data={"kv": 1, "values": [stale]}, content_type="application/json")
    assert res.status_code == 409 and res.json()["error"] == "zk_value_changed"
    assert _stored("CANOPY_WEB_PAT") == read
    ok = {**settled, "expect": zk.value_digest(read)}
    res = fleet.client.put(f"{ZK}/values", data={"kv": 1, "values": [ok]}, content_type="application/json")
    assert res.status_code == 200, res.content
    assert _stored("CANOPY_WEB_PAT").startswith("zk1:")
    assert fleet.client.get(ZK).json()["sealed"] == []


def test_expect_empty_means_no_value_stored(fleet):
    assert _enable(fleet).status_code == 200
    new = {**zk_values(fleet, {"brand-new": "v"})[0], "expect": ""}
    assert fleet.client.put(f"{ZK}/values", data={"kv": 1, "values": [new]},
                            content_type="application/json").status_code == 200
    again = {**zk_values(fleet, {"brand-new": "v2"})[0], "expect": ""}
    res = fleet.client.put(f"{ZK}/values", data={"kv": 1, "values": [again]}, content_type="application/json")
    assert res.status_code == 409 and res.json()["error"] == "zk_value_changed"


def test_enable_and_rotate_refuse_an_unsettled_value(fleet):
    assert _enable(fleet).status_code == 200
    services.set_agent_credentials(_agent(fleet), {"CANOPY_WEB_PAT": "minted"}, minted=True)
    body = {"new_kv": 2, "wraps": [member_wrap(fleet, kv=2), device_wrap(fleet, kv=2), recovery_wrap(fleet, kv=2)],
            "values": [{"name": "CANOPY_WEB_PAT", "value_zk": _stored("CANOPY_WEB_PAT")},
                       *zk_values(fleet, {"kindora": "kin-value"}, kv=2)]}
    res = fleet.client.post(f"{ZK}/rotate", data=body, content_type="application/json")
    assert res.status_code == 422


def test_sealing_with_nobody_to_read_it_is_refused(fleet):
    agent = _agent(fleet)
    Agent.objects.filter(pk=agent.pk).update(zk_enabled=True, zk_kv=0)
    RunnerDeviceKey.objects.all().delete()
    UserVaultKeyset.objects.all().delete()
    with pytest.raises(zk.ZkError) as exc:
        services.set_agent_credentials(_agent(fleet), {"gog-token": "t"}, minted=True)
    assert exc.value.code == "zk_no_recipients"


# ---- born zero-knowledge (kv 0) ------------------------------------------------------

def _upsert(slug, ws, owner):
    return services.upsert_agent(SimpleNamespace(slug=slug, name=slug, description="", persona="", email="",
                                                 avatar_url=""), workspace=ws, owner=owner)


def test_every_new_agent_is_born_zk_with_or_without_a_vault_key(fleet):
    born = _upsert("newbie", fleet.ws, fleet.user)
    assert born.zk_enabled and born.zk_kv == 0
    other = get_user_model().objects.create_user(username="nokey", email="nokey@dimagi.com")
    nokey = _upsert("plain", fleet.ws, other)
    assert nokey.zk_enabled and nokey.zk_kv == 0
    # An existing agent is never switched by an upsert.
    _upsert("eva", fleet.ws, fleet.user)
    assert not _agent(fleet).zk_enabled


def test_kv0_seals_every_write_then_enable_creates_the_vault_key(fleet):
    Agent.objects.filter(pk=fleet.agent.pk).update(zk_enabled=True, zk_kv=0)
    AgentCredential.objects.all().delete()
    # A plain write (the canopy CLI's custodial import) is sealed, not refused.
    res = fleet.client.put("/api/agents/eva/credentials", data={"values": {"kindora": "kin"}},
                           content_type="application/json")
    assert res.status_code == 200, res.content
    stored = _stored("kindora")
    assert stored.startswith("zks1:") and AgentCredential.objects.get(name="kindora").kv == 0
    # A zk1 value cannot exist yet; a zks1 one from a browser can.
    res = fleet.client.put(f"{ZK}/values", data={"kv": 0, "values": zk_values(fleet, {"x": "y"}, kv=0)},
                           content_type="application/json")
    assert res.status_code == 409 and res.json()["error"] == "zk_no_vault_key"
    assert fleet.client.get(ZK).json()["sealed"] == ["kindora"]
    # A device proves itself and reads it with its own key — no wrap exists at kv 0.
    body = _resolve(fleet).json()
    assert body["wrap"] is None and body["kv"] == 0
    plain = ref.open_sealed_value(body["values"]["kindora"], _aid(fleet), "kindora", "member",
                                  str(fleet.user.pk), fleet.member_priv)
    assert plain == "kin"
    # The owner creates the vault key: every value settled into zk1.
    res = _enable(fleet, values=zk_values(fleet, {"kindora": plain}))
    assert res.status_code == 200, res.content
    agent = _agent(fleet)
    assert agent.zk_enabled and agent.zk_kv == 1 and _stored("kindora").startswith("zk1:")


# ---- canopy-pat rotation on a ZK agent -------------------------------------------------

def test_pat_rotation_seals_the_new_token_and_retires_the_one_it_minted(fleet):
    login = get_user_model().objects.create_user(username="eva@dimagi-ai.com", email="eva@dimagi-ai.com")
    WorkspaceMembership.objects.create(workspace=fleet.ws, user=login, role=WorkspaceMembership.EDITOR)
    Agent.objects.filter(pk=fleet.agent.pk).update(user=login)
    assert _enable(fleet).status_code == 200
    assert pat_rotation.enable_refusal(_agent(fleet), fleet.user) is None
    first = pat_rotation.rotate(_agent(fleet), trigger="manual", actor=fleet.user)
    assert first.status == AgentPatRotation.ROTATED and first.old_token is None
    raw = ref.open_sealed_value(_stored("CANOPY_WEB_PAT"), _aid(fleet), "CANOPY_WEB_PAT", "device",
                                fleet.device.ecdh_jkt, fleet.ecdh)
    assert hashlib.sha256(raw.encode()).hexdigest() == first.new_token.token_hash
    second = pat_rotation.rotate(_agent(fleet), trigger="manual", actor=fleet.user)
    assert second.old_token_id == first.new_token_id and second.revoke_after is not None
    assert PersonalToken.objects.filter(user=login, revoked_at__isnull=True).count() == 2


# ---- approve = wrap ---------------------------------------------------------------------

def test_a_pending_keys_listing_names_the_zk_agents_approving_it_will_need(fleet):
    assert _enable(fleet).status_code == 200
    sign, ecdh = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    key = _register(fleet, sign, ecdh)
    rows = {r["id"]: r for r in fleet.client.get("/api/runner-device-keys/").json()}
    assert rows[str(key.pk)]["status"] == "pending"
    assert rows[str(key.pk)]["zk_pending_agents"] == ["eva"]
    # The already-wrapped active key needs nothing.
    assert rows[str(fleet.device.pk)]["zk_pending_agents"] == []
    # An agent with no vault key yet and nothing stored has nothing to grant.
    Agent.objects.filter(pk=fleet.agent.pk).update(zk_kv=0)
    AgentCredential.objects.filter(agent=fleet.agent).delete()
    rows = {r["id"]: r for r in fleet.client.get("/api/runner-device-keys/").json()}
    assert rows[str(key.pk)]["zk_pending_agents"] == []


def test_an_approved_key_that_came_to_serve_an_agent_since_offers_give_access(fleet):
    """A machine approved before it served the agent (made private later, a new
    license) is not pending, yet waits on the agent: its row still lists it, so
    Settings offers "Give access" — on both the account and the agent's listing."""
    assert _enable(fleet).status_code == 200
    sign, ecdh = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    key = _register(fleet, sign, ecdh)
    RunnerDeviceKey.objects.filter(pk=key.pk).update(status=RunnerDeviceKey.ACTIVE, approved_at=timezone.now(),
                                                    ecdh_approved_at=timezone.now())
    rows = {r["id"]: r for r in fleet.client.get("/api/runner-device-keys/").json()}
    assert (rows[str(key.pk)]["status"], rows[str(key.pk)]["ecdh_waiting"]) == ("active", False)
    assert rows[str(key.pk)]["zk_pending_agents"] == ["eva"]
    assert rows[str(key.pk)]["can_open"] == []
    assert rows[str(fleet.device.pk)]["zk_pending_agents"] == []  # already wrapped
    on_agent = {r["id"]: r for r in fleet.client.get("/api/agents/eva/device-keys").json()}
    assert on_agent[str(key.pk)]["zk_pending_agents"] == ["eva"]


def test_approving_a_key_covers_a_born_agent_whose_sealed_values_it_cannot_read(fleet):
    """kv 0: values sealed before this key existed are listed, so the approval
    touch creates the vault key; a value already sealed to the key is not."""
    sign, ecdh = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    key = _register(fleet, sign, ecdh)
    Agent.objects.filter(pk=fleet.agent.pk).update(zk_enabled=True, zk_kv=0)
    AgentCredential.objects.filter(agent=fleet.agent).delete()
    old = zk_seal.seal_to("v", agent_id=str(fleet.agent.pk), name="kindora", kv=0,
                          recipients=[("device", fleet.device.ecdh_jkt, fleet.device.ecdh_public_jwk)])
    AgentCredential.objects.create(agent=fleet.agent, name="kindora", value_zk=old)
    rows = {r["id"]: r for r in fleet.client.get("/api/runner-device-keys/").json()}
    assert rows[str(key.pk)]["zk_pending_agents"] == ["eva"]
    assert rows[str(fleet.device.pk)]["zk_pending_agents"] == []  # it can already read it


# ---- telling vault-key holders a target is waiting (zk_notify) --------------------------

def test_a_pending_device_notifies_each_holder_once_after_the_grace_then_clears(fleet, mailoutbox):
    from apps.agents import zk_notify
    from apps.agents.models import ZkGrantNotice

    assert _enable(fleet, wraps=[member_wrap(fleet), recovery_wrap(fleet)]).status_code == 200
    t0 = timezone.now()
    # Inside the grace: an Approve usually grants in the same click — nobody is told.
    assert zk_notify.sweep(t0) == 0
    assert mailoutbox == []
    notice = ZkGrantNotice.objects.get(agent=fleet.agent)
    assert (notice.kind, notice.target, notice.kv) == ("device", str(fleet.device.pk), 1)
    # Past it: the one holder (jj, member wrap at kv 1) is told, with the deep link.
    assert zk_notify.sweep(t0 + zk_notify.GRACE) == 1
    assert [m.to for m in mailoutbox] == [["jj@dimagi.com"]]
    assert "/agents/eva/settings#cred-zk" in mailoutbox[0].body
    assert "box" in mailoutbox[0].subject
    # Once per target.
    assert zk_notify.sweep(t0 + 2 * zk_notify.GRACE) == 0
    assert len(mailoutbox) == 1
    # Granted: the notice closes.
    fleet.client.post(f"{ZK}/wraps", data={"kv": 1, "wraps": [device_wrap(fleet)]},
                      content_type="application/json")
    zk_notify.sweep(t0 + 3 * zk_notify.GRACE)
    notice.refresh_from_db()
    assert notice.cleared_at is not None


def test_a_device_granted_within_the_grace_is_never_mentioned(fleet, mailoutbox):
    from apps.agents import zk_notify

    assert _enable(fleet, wraps=[member_wrap(fleet), recovery_wrap(fleet)]).status_code == 200
    t0 = timezone.now()
    zk_notify.sweep(t0)
    fleet.client.post(f"{ZK}/wraps", data={"kv": 1, "wraps": [device_wrap(fleet)]},
                      content_type="application/json")
    assert zk_notify.sweep(t0 + zk_notify.GRACE) == 0
    assert mailoutbox == []


def test_a_new_admin_with_a_keyset_is_a_target_and_only_holders_are_told(fleet, mailoutbox):
    from apps.agents import zk_notify

    assert _enable(fleet).status_code == 200  # device wrapped too: nothing pending
    other = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet.ws, user=other, role=WorkspaceMembership.EDITOR)
    _make_keyset(other, _add_passkey(other, "o-cred").credential_id)
    AgentAdmin.objects.create(agent=fleet.agent, user=other, granted_by=fleet.user)
    t0 = timezone.now()
    zk_notify.sweep(t0)
    assert zk_notify.sweep(t0 + zk_notify.GRACE) == 1
    assert [m.to for m in mailoutbox] == [["jj@dimagi.com"]]  # not the new admin: they hold nothing yet
    assert "o@dimagi.com" in mailoutbox[0].body


def test_custodial_agents_are_not_swept(fleet, mailoutbox):
    from apps.agents import zk_notify
    from apps.agents.models import ZkGrantNotice

    assert zk_notify.sweep(timezone.now() + zk_notify.GRACE) == 0
    assert not ZkGrantNotice.objects.exists()


def test_the_heartbeat_sweep_is_throttled_and_never_raises(fleet, monkeypatch):
    from apps.agents import zk_notify

    calls = []
    monkeypatch.setattr(zk_notify, "sweep", lambda now=None: calls.append(now) or 0)
    zk_notify.maybe_sweep()
    zk_notify.maybe_sweep()
    assert len(calls) == 1
    cache.clear()
    monkeypatch.setattr(zk_notify, "sweep", lambda now=None: 1 / 0)
    zk_notify.maybe_sweep()  # swallowed


# ---- one recovery code per person (§4c) -------------------------------------------------

def _add_recovery_seal(f, r=None):
    scalar = f.member_priv.private_numbers().private_value.to_bytes(32, "big")
    seal = ref.seal_user_key_recovery(scalar, str(f.user.pk), r or f.recovery, os.urandom(16), os.urandom(12))
    res = f.client.put("/api/vault/keyset", data={"public_jwk": f.keyset.public_jwk,
                                                  "seals": f.keyset.seals + [seal]},
                       content_type="application/json")
    assert res.status_code == 200, res.content
    f.keyset.refresh_from_db()
    return seal


def test_shape_checks_accept_the_person_recovery_seal():
    zk.check_seal(VECTORS["outputs"]["user_key_seal_recovery"])
    with pytest.raises(zk.ZkError):
        zk.check_seal(VECTORS["outputs"]["user_key_seal_recovery"] | {"cred": "x"})


def test_the_person_recovery_code_opens_the_keyset(fleet):
    seal = _add_recovery_seal(fleet)
    code = ref.recovery_code(fleet.recovery)
    scalar = ref.open_user_key_recovery(seal, str(fleet.user.pk), ref.parse_recovery_code(code))
    assert scalar == fleet.member_priv.private_numbers().private_value.to_bytes(32, "big")
    assert [s["alg"] for s in fleet.keyset.seals] == ["prf", "recovery"]


def test_a_keyset_holds_at_most_one_recovery_seal_and_never_only_that(fleet):
    seal = ref.seal_user_key_recovery(b"\0" * 32, str(fleet.user.pk), fleet.recovery, os.urandom(16), os.urandom(12))
    for seals in (fleet.keyset.seals + [seal, seal], [seal]):
        res = fleet.client.put("/api/vault/keyset", data={"public_jwk": fleet.keyset.public_jwk, "seals": seals},
                               content_type="application/json")
        assert res.status_code == 422, res.content


def test_enable_needs_no_agent_recovery_wrap_when_the_member_has_a_recovery_code(fleet):
    _add_recovery_seal(fleet)
    res = _enable(fleet, wraps=[member_wrap(fleet), device_wrap(fleet)])
    assert res.status_code == 200, res.content
    kinds = sorted(AgentVaultWrap.objects.filter(agent=fleet.agent).values_list("kind", flat=True))
    assert kinds == ["device", "member"]


def test_enable_still_needs_a_recovery_without_the_member_recovery_code(fleet):
    res = _enable(fleet, wraps=[device_wrap(fleet)])
    assert res.status_code == 422 and res.json()["error"] == "zk_recovery_required", res.content


def test_rotate_needs_no_agent_recovery_wrap_when_the_member_has_a_recovery_code(fleet):
    _add_recovery_seal(fleet)
    assert _enable(fleet).status_code == 200
    vk2 = os.urandom(32)
    res = _rotate(fleet, [member_wrap(fleet, kv=2, vk=vk2), device_wrap(fleet, kv=2, vk=vk2)], vk=vk2)
    assert res.status_code == 200, res.content


# ---- the canopy-web vault has no custodial mode ---------------------------------------------

def test_disable_is_gone(fleet):
    assert _enable(fleet).status_code == 200
    res = fleet.client.post(f"{ZK}/disable", data={"values": []}, content_type="application/json")
    assert res.status_code in (404, 405)
    assert _agent(fleet).zk_enabled


def test_custodial_agents_are_listed_never_converted(fleet):
    from django.core.management import call_command
    from io import StringIO

    from apps.agents.management.commands.list_custodial_vault_agents import custodial_vault_agents

    rows = custodial_vault_agents()
    assert [r["slug"] for r in rows] == ["eva"] and rows[0]["custodial_values"] == ["CANOPY_WEB_PAT", "kindora"]
    out = StringIO()
    call_command("list_custodial_vault_agents", stdout=out)
    assert "eva" in out.getvalue() and "kin-value" not in out.getvalue()
    assert not _agent(fleet).zk_enabled  # reported, not converted
    assert _enable(fleet).status_code == 200
    assert custodial_vault_agents() == []


def test_make_zero_knowledge_seals_every_custodial_value(fleet):
    assert services.make_zero_knowledge(_agent(fleet)) == 2
    agent = _agent(fleet)
    assert agent.zk_enabled and agent.zk_kv == 0
    for name, plain in PLAIN.items():
        row = AgentCredential.objects.get(name=name)
        assert row.value_enc == ""
        assert ref.open_sealed_value(row.value_zk, _aid(fleet), name, "device", fleet.device.ecdh_jkt,
                                     fleet.ecdh) == plain


def test_migration_restores_a_key_sent_back_to_pending_by_its_ecdh_key(fleet):
    """0079: an approved key that `_set_ecdh` used to send back to pending signs in
    again; its ECDH key waits. A plainly active key keeps its ECDH approval."""
    import importlib

    from django.apps import apps as django_apps

    forwards = importlib.import_module("apps.harness.migrations.0079_device_key_ecdh_approval").forwards
    bumped = _register(fleet, ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1()))
    RunnerDeviceKey.objects.filter(pk=bumped.pk).update(status=RunnerDeviceKey.PENDING, approved_at=timezone.now())
    fresh = _register(fleet, ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1()))
    RunnerDeviceKey.objects.filter(pk=fleet.device.pk).update(ecdh_approved_at=None)
    forwards(django_apps, None)
    bumped.refresh_from_db(), fresh.refresh_from_db(), fleet.device.refresh_from_db()
    assert bumped.status == RunnerDeviceKey.ACTIVE and bumped.ecdh_waiting
    assert fresh.status == RunnerDeviceKey.PENDING  # never approved: still a new box
    assert fleet.device.ecdh_ready


# ==== an agent in ANOTHER workspace the box is enrolled in (marshellis, 2026-10-18) ====

def test_a_new_agent_in_another_enrolled_workspace_seals_to_the_box(fleet):
    """Jonathan's runners serve his family's workspace too. A new agent there has no
    runner rows of its own; it follows that workspace's runner order. Its secrets
    must seal to the approved machines that order names — and to nobody else."""
    from apps.harness import device_keys
    from apps.harness.models import WorkspaceRunnerOrder
    from apps.harness.runners import enroll_runner, unenroll_runner

    f = fleet
    family = Workspace.objects.create(slug="family", display_name="Family", created_by=f.user)
    WorkspaceMembership.objects.create(workspace=family, user=f.user, role=WorkspaceMembership.OWNER)
    helper = Agent.objects.create(slug="helper", name="Helper", workspace=family, owner=f.user,
                                  credential_source=Agent.CANOPY_WEB)
    # Not enrolled there yet: the box is no recipient, whatever the order says.
    assert zk.eligible_device_keys(helper) == []

    # A cloud runner serves one tree (`runners._other_tree`): the family tree is
    # this box's unit only once it leaves its first one.
    unenroll_runner(f.runner, f.ws.slug)
    enroll_runner(f.runner, "family", by=f.user)
    WorkspaceRunnerOrder.objects.create(workspace=family, runner=f.runner, rank=0)

    assert [k.pk for k in zk.eligible_device_keys(helper)] == [f.device.pk]
    assert ("device", f.device.ecdh_jkt) in {(kind, t) for kind, t, _ in zk_seal.recipients_for(helper)}
    assert "helper" in {a.slug for a in device_keys.agents_served(f.runner)}
