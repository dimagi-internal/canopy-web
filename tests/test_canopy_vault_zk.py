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

from apps.agents.credentials import pat_rotation, zk, zk_seal
from apps.agents import services
from apps.agents.models import (
    Agent,
    AgentAdmin,
    AgentCredential,
    AgentCredentialAlert,
    AgentCredentialRead,
    AgentPatRotation,
    AgentVaultWrap,
    Vault,
    VaultKeyWrap,
    VaultSecret,
)
from apps.agents.schemas import ZkWrapIn
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
    # Stored as `canopy vault import` stores them: sealed to the agent's readers (the
    # approved device, the owner's keyset), the agent zero-knowledge at kv 0.
    res = client.put("/api/agents/eva/credentials", data={"values": {"CANOPY_WEB_PAT": "pat-value",
                                                                     "kindora": "kin-value"}},
                     content_type="application/json")
    assert res.status_code == 200, res.content
    f.agent.refresh_from_db()
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


def _legacy(f) -> None:
    """Make eva a legacy 1Password agent that never stored a value here: the one kind
    that is not zero-knowledge."""
    AgentCredential.objects.filter(agent=f.agent).delete()
    Agent.objects.filter(pk=f.agent.pk).update(zk_enabled=False, zk_kv=0, credential_source=Agent.ONEPASSWORD)


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

def test_enable_settles_every_value_under_the_new_vault_key(fleet):
    res = _enable(fleet)
    assert res.status_code == 200, res.content
    state = res.json()
    assert state["enabled"] and state["kv"] == 1 and state["agent_id"] == _aid(fleet)
    assert sorted(w["kind"] for w in state["wraps"]) == ["device", "member", "recovery"]
    assert state["pending"] == [] and state["values"] is None
    dev = next(w for w in state["wraps"] if w["kind"] == "device")
    assert dev["public_jwk"] == ref.jwk(fleet.ecdh.public_key())
    for cred in AgentCredential.objects.filter(agent=fleet.agent):
        assert cred.value_zk.startswith("zk1:") and cred.kv == 1
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
    assert Agent.objects.get(pk=fleet.agent.pk).zk_kv == 0
    assert AgentCredential.objects.get(name="CANOPY_WEB_PAT").value_zk.startswith("zks1:")


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


# ---- after enable: a plaintext write is sealed ------------------------------------------------

def test_a_plaintext_write_is_sealed_on_a_zk_agent(fleet):
    assert _enable(fleet).status_code == 200
    # There is no reveal: canopy-web cannot open a value.
    assert fleet.client.post("/api/agents/eva/credentials/CANOPY_WEB_PAT/reveal").status_code in (404, 405)
    # Zero-knowledge is the only mode: `canopy vault import` writes in plaintext
    # and canopy-web seals the value to the agent's readers, keeping no copy.
    res = fleet.client.put("/api/agents/eva/credentials", data={"values": {"CANOPY_WEB_PAT": "x"}},
                           content_type="application/json")
    assert res.status_code == 200, res.content
    row = AgentCredential.objects.get(name="CANOPY_WEB_PAT")
    assert row.value_zk.startswith("zks1:") and row.kv == 1
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
    # Scoped by `?refs=`.
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
    # Says whose machine, what is missing and that it was never given the key.
    assert alert.user.email in alert.detail and "never given" in alert.detail
    assert not AgentCredentialRead.objects.exists()


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


def test_storing_a_value_declares_its_name(fleet):
    assert _enable(fleet).status_code == 200
    vals = zk_values(fleet, {"BAILEY_GH_APP_ID": "12345"})
    res = fleet.client.put(f"{ZK}/values", data={"kv": 1, "values": vals}, content_type="application/json")
    assert res.status_code == 200
    fleet.agent.refresh_from_db()
    assert fleet.agent.runtime_secrets == ["CANOPY_WEB_PAT", "kindora", "BAILEY_GH_APP_ID"]
    # Writing it again adds nothing.
    fleet.client.put(f"{ZK}/values", data={"kv": 1, "values": vals}, content_type="application/json")
    fleet.agent.refresh_from_db()
    assert fleet.agent.runtime_secrets.count("BAILEY_GH_APP_ID") == 1


def test_before_enable_every_eligible_principal_is_pending(fleet):
    _legacy(fleet)
    state = fleet.client.get(ZK).json()
    assert not state["enabled"]
    assert sorted((p["kind"], p["target"]) for p in state["pending"]) == sorted(
        [("device", str(fleet.device.pk)), ("member", str(fleet.user.pk))])


def test_values_put_refuses_an_agent_that_is_not_zk(fleet):
    _legacy(fleet)
    res = fleet.client.put(f"{ZK}/values", data={"kv": 1, "values": zk_values(fleet, {"a": "b"})},
                           content_type="application/json")
    assert res.status_code == 409 and res.json()["error"] == "zk_not_enabled"


def test_grant_pending_devices_and_members(fleet):
    assert _enable(fleet, wraps=[member_wrap(fleet), recovery_wrap(fleet)]).status_code == 200
    other = get_user_model().objects.create_user(username="o", email="o@dimagi.com")
    ms = WorkspaceMembership.objects.create(workspace=fleet.ws, user=other, role=WorkspaceMembership.VIEWER)
    o_priv, _ = _make_keyset(other, _add_passkey(other, "o-cred").credential_id)
    # A viewer has no full access to the agent, so is not a member of its vault:
    # not pending, and not grantable.
    state = fleet.client.get(ZK).json()
    assert [(p["kind"], p["target"]) for p in state["pending"]] == [("device", str(fleet.device.pk))]
    assert state["pending"][0]["public_jwk"] == ref.jwk(fleet.ecdh.public_key())
    assert state["pending"][0]["ecdh_jkt"] == fleet.device.ecdh_jkt
    res = fleet.client.post(f"{ZK}/wraps", data={"kv": 1, "wraps": [
        member_wrap(fleet, user=other, pub=o_priv.public_key())]}, content_type="application/json")
    assert res.status_code == 422 and res.json()["error"] == "zk_invalid_target"
    # An editor has full access, so the agent's vault link makes them a member.
    ms.role = WorkspaceMembership.EDITOR
    ms.save()
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
    from apps.agents.credentials import zk

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
    """Settings > Your runners reads "box — AWS server · key held by AWS · can
    open Eva" from these fields."""
    def row():
        return fleet.client.get("/api/runner-device-keys/").json()[0]

    _legacy(fleet)
    assert row()["can_open"] == []  # not zero-knowledge: nothing to open
    assert row()["runner_kind"] == "cloud" and row()["runner_host"] == ""
    Agent.objects.filter(pk=fleet.agent.pk).update(credential_source=Agent.CANOPY_WEB)
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
    assert stored.startswith("zks1:")
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
    _legacy(fleet)
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
    # The agent's setup step links straight to that machine's row, where one touch lets it in.
    from apps.agents.setup import _vault
    step = _vault(_agent(fleet), "/w/x/agents/eva/settings")
    assert step["href"] == f"/settings?device_key={key.pk}#your-runners"
    assert step["action"] == f"Let {key.runner.name} in"


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
    from apps.agents.credentials import zk_notify
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
    assert "/agents/eva/settings/security#cred-zk" in mailoutbox[0].body
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
    from apps.agents.credentials import zk_notify

    assert _enable(fleet, wraps=[member_wrap(fleet), recovery_wrap(fleet)]).status_code == 200
    t0 = timezone.now()
    zk_notify.sweep(t0)
    fleet.client.post(f"{ZK}/wraps", data={"kv": 1, "wraps": [device_wrap(fleet)]},
                      content_type="application/json")
    assert zk_notify.sweep(t0 + zk_notify.GRACE) == 0
    assert mailoutbox == []


def test_a_new_admin_with_a_keyset_is_a_target_and_only_holders_are_told(fleet, mailoutbox):
    from apps.agents.credentials import zk_notify

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
    from apps.agents.credentials import zk_notify
    from apps.agents.models import ZkGrantNotice

    assert zk_notify.sweep(timezone.now() + zk_notify.GRACE) == 0
    assert not ZkGrantNotice.objects.exists()


def test_the_heartbeat_sweep_is_throttled_and_never_raises(fleet, monkeypatch):
    from apps.agents.credentials import zk_notify

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
    # this runner's tree only once it leaves its first one.
    unenroll_runner(f.runner, f.ws.slug)
    enroll_runner(f.runner, "family", by=f.user)
    WorkspaceRunnerOrder.objects.create(workspace=family, runner=f.runner, rank=0)

    assert [k.pk for k in zk.eligible_device_keys(helper)] == [f.device.pk]
    assert ("device", f.device.ecdh_jkt) in {(kind, t) for kind, t, _ in zk_seal.recipients_for(helper)}
    assert "helper" in {a.slug for a in device_keys.agents_served(f.runner)}


# ---- the keeper: a box that holds the vault key passes it on -----------------------------

def _keeper_url(fleet, suffix=""):
    return f"/api/harness/runners/{fleet.runner.pk}/vault-keeper{suffix}"


def _editor_with_a_key(fleet, name="ed"):
    user = get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet.ws, user=user, role=WorkspaceMembership.EDITOR)
    priv, _ = _make_keyset(user, _add_passkey(user, f"{name}-cred").credential_id)
    return user, priv


def test_a_holder_box_is_told_who_waits_and_adds_their_copy(fleet):
    assert _enable(fleet).status_code == 200
    assert fleet.client.get(_keeper_url(fleet)).json() == {"agents": [], "delegated": []}  # nobody waiting
    user, priv = _editor_with_a_key(fleet)
    body = fleet.client.get(_keeper_url(fleet)).json()
    [work] = body["agents"]
    assert work["agent"] == "eva" and work["kv"] == 1 and work["device_key"] == str(fleet.device.pk)
    assert [(p["kind"], p["target"]) for p in work["pending"]] == [("member", str(user.pk))]
    res = fleet.client.post(_keeper_url(fleet, "/eva/wraps"), data={"kv": 1, "wraps": [
        member_wrap(fleet, user=user, pub=priv.public_key())]}, content_type="application/json")
    assert res.status_code == 200 and res.json() == {"granted": 1}, res.content
    assert AgentVaultWrap.objects.filter(agent=fleet.agent, kind="member", user=user).exists()
    assert fleet.client.get(_keeper_url(fleet)).json() == {"agents": [], "delegated": []}


def test_a_keeper_never_replaces_a_copy(fleet):
    assert _enable(fleet).status_code == 200
    res = fleet.client.post(_keeper_url(fleet, "/eva/wraps"), data={"kv": 1, "wraps": [member_wrap(fleet)]},
                            content_type="application/json")
    assert res.status_code == 409 and res.json()["error"] == "zk_not_pending"


def test_a_box_with_no_copy_is_no_keeper(fleet):
    assert _enable(fleet, wraps=[member_wrap(fleet), recovery_wrap(fleet)]).status_code == 200
    user, priv = _editor_with_a_key(fleet)
    assert fleet.client.get(_keeper_url(fleet)).json() == {"agents": [], "delegated": []}
    res = fleet.client.post(_keeper_url(fleet, "/eva/wraps"), data={"kv": 1, "wraps": [
        member_wrap(fleet, user=user, pub=priv.public_key())]}, content_type="application/json")
    assert res.status_code == 403 and res.json()["error"] == "zk_not_a_holder"

# ---- key copies follow vault membership (spec 2026-10-22-vault-access-follows-canopy-access-design.md) --

def _leave_the_vault(fleet):
    """Make the fleet's owner (the box's person) NOT a member: someone else manages
    the vault by name, and the vault stops following the agent's access list."""
    from apps.agents.credentials import vault_members

    other = get_user_model().objects.create_user(username="mgr", email="mgr@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet.ws, user=other, role=WorkspaceMembership.VIEWER)
    vault = vault_members.vault_for_agent(fleet.agent)
    vault_members.add_member(vault, other, role="admin", by=fleet.user)
    vault_members.set_agent_link(vault, fleet.agent, linked=False)
    return vault


def test_a_box_whose_person_left_the_vault_reads_nothing(fleet):
    assert _enable(fleet).status_code == 200
    assert _resolve(fleet).status_code == 200
    _leave_the_vault(fleet)
    res = _resolve(fleet)
    assert res.status_code == 403 and res.json()["error"] == "not_a_vault_member"


def test_an_admin_who_left_the_vault_gets_no_ciphertext_though_they_kept_a_copy(fleet):
    # Checked on every read, not swept: their old copy stays in the table, and
    # opens nothing because canopy-web will not hand them the values.
    assert _enable(fleet).status_code == 200
    assert fleet.client.get(f"{ZK}?values=true").status_code == 200
    _leave_the_vault(fleet)
    assert AgentVaultWrap.objects.filter(agent=fleet.agent, kind="member", user=fleet.user).exists()
    res = fleet.client.get(f"{ZK}?values=true")
    assert res.status_code == 403 and res.json()["error"] == "not_a_vault_member"
    assert fleet.client.get(ZK).status_code == 200   # the public state, no ciphertext


def test_a_box_whose_person_left_is_no_keeper_and_nobody_who_left_is_a_holder(fleet):
    from apps.agents.credentials import vault_keeper, zk_notify

    assert _enable(fleet).status_code == 200
    fleet.agent.refresh_from_db()
    assert [w.agent_id for w in vault_keeper._held(fleet.runner)] == [fleet.agent.pk]
    assert zk_notify.holders(fleet.agent) == [fleet.user]
    _leave_the_vault(fleet)
    assert vault_keeper._held(fleet.runner) == []
    assert zk_notify.holders(fleet.agent) == []


def test_an_agent_admin_who_left_the_vault_cannot_rotate_its_key_to_their_own_box(fleet):
    # The owner is still the AGENT's admin; without the vault-admin check they could
    # rotate to a key wrapped for a runner they own, then read every value written after.
    assert _enable(fleet).status_code == 200
    _leave_the_vault(fleet)
    vk2 = os.urandom(32)
    res = _rotate(fleet, [device_wrap(fleet, kv=2, vk=vk2), recovery_wrap(fleet, kv=2, vk=vk2)],
                  values=zk_values(fleet, PLAIN, kv=2, vk=vk2))
    assert res.status_code == 403
    fleet.agent.refresh_from_db()
    assert fleet.agent.zk_kv == 1
    res = fleet.client.put(f"{ZK}/values", data={"kv": 1, "values": []}, content_type="application/json")
    assert res.status_code == 403


def test_coming_back_to_the_vault_opens_it_again_with_the_same_copy(fleet):
    from apps.agents.credentials import vault_members

    assert _enable(fleet).status_code == 200
    vault = _leave_the_vault(fleet)
    assert _resolve(fleet).status_code == 403
    vault_members.set_agent_link(vault, fleet.agent, linked=True)
    assert _resolve(fleet).status_code == 200


# ---- workspace vaults (spec 2026-10-22-vault-access-follows-canopy-access-design.md §4) ------

def _ws_vault(fleet, name="Shared"):
    res = fleet.client.post("/api/workspaces/connect/vaults", data={"name": name}, content_type="application/json")
    assert res.status_code == 200, res.content
    return res.json()["vault_id"]


def _ws_wraps(fleet, binding, vk, kv=1):
    dev = {"kind": "device", "target": str(fleet.device.pk), "wrap": ref.wrap_ecdh_es(
        vk, fleet.ecdh.public_key(), ref.device_info(binding, kv, fleet.device.ecdh_jkt),
        ec.generate_private_key(ec.SECP256R1()), os.urandom(12))}
    mem = {"kind": "member", "target": str(fleet.user.pk), "wrap": ref.wrap_ecdh_es(
        vk, fleet.member_priv.public_key(), ref.member_info(binding, kv, str(fleet.user.pk)),
        ec.generate_private_key(ec.SECP256R1()), os.urandom(12))}
    rec = {"kind": "recovery", "target": None,
           "wrap": ref.wrap_recovery(vk, binding, kv, os.urandom(32), os.urandom(16), os.urandom(12))}
    return [mem, dev, rec]


def _ws_enabled(fleet, plain, *, attach=True):
    vid = _ws_vault(fleet)
    if attach:
        assert fleet.client.post(f"/api/vaults/{vid}/attachments", data={"agent": "eva"},
                                 content_type="application/json").status_code == 200
    state = fleet.client.get(f"/api/vaults/{vid}/zk").json()
    binding, vk = state["agent_id"], os.urandom(32)
    values = [{"name": n, "value_zk": ref.encrypt_value(vk, binding, n, 1, v, os.urandom(12))}
              for n, v in plain.items()]
    res = fleet.client.post(f"/api/vaults/{vid}/zk/enable", content_type="application/json",
                            data={"kv": 1, "wraps": _ws_wraps(fleet, binding, vk), "values": values})
    assert res.status_code == 200, res.content
    return vid, binding, vk


def test_a_workspace_vault_starts_with_its_owners_and_attached_users(fleet):
    vid = _ws_vault(fleet)
    body = fleet.client.get(f"/api/vaults/{vid}").json()
    assert body["kv"] == 0 and body["you"] == "owner"
    assert [(m["email"], m["role"], m["sources"]) for m in body["members"]] == [
        ("jj@dimagi.com", "owner", ["workspace_owners:connect"])]
    state = fleet.client.get(f"/api/vaults/{vid}/zk").json()
    assert state["agent_id"] == f"vault:{vid}" and not state["enabled"]
    # Attached to no agent yet: no machine serves it, so only the person is pending.
    assert [p["kind"] for p in state["pending"]] == ["member"]
    fleet.client.post(f"/api/vaults/{vid}/attachments", data={"agent": "eva"}, content_type="application/json")
    kinds = sorted(p["kind"] for p in fleet.client.get(f"/api/vaults/{vid}/zk").json()["pending"])
    assert kinds == ["device", "member"]


def test_a_box_reads_a_workspace_vault_layered_under_its_agent(fleet):
    assert _enable(fleet).status_code == 200
    vid, binding, vk = _ws_enabled(fleet, {"gog-oauth-client": "client-json", "kindora": "shared-kin"})
    body = _resolve(fleet).json()
    [layer] = body["vaults"]
    assert layer["vault_id"] == vid and layer["binding_id"] == binding and layer["kv"] == 1
    got = ref.unwrap_ecdh_es(layer["wrap"], fleet.ecdh, ref.device_info(binding, 1, fleet.device.ecdh_jkt))
    assert got == vk
    assert ref.decrypt_value(vk, binding, "gog-oauth-client", 1, layer["values"]["gog-oauth-client"]) == "client-json"
    # The agent's own values are still its own (the client lets them win a clash).
    assert set(body["values"]) == set(PLAIN)
    names = set(AgentCredentialRead.objects.filter(via__isnull=False).values_list("name", flat=True))
    assert "Shared/gog-oauth-client" in names


def test_a_workspace_vault_serves_only_what_it_is_attached_to(fleet):
    assert _enable(fleet).status_code == 200
    vid, _, _ = _ws_enabled(fleet, {"x": "y"})
    [att] = fleet.client.get(f"/api/vaults/{vid}").json()["attachments"]
    res = fleet.client.delete(f"/api/vaults/{vid}/attachments/{att['id']}")
    assert res.status_code == 200
    # Detached: the box serves no agent it is attached to, so it reads nothing and is
    # no keeper of it — whatever copy it still holds.
    from apps.agents.credentials import vault_keeper

    assert _resolve(fleet).json()["vaults"] == []
    assert vault_keeper._held_workspace(fleet.runner) == []


def test_only_a_workspace_owner_makes_one_and_only_a_manager_changes_it(fleet):
    other = get_user_model().objects.create_user(username="ed2", email="ed2@dimagi.com")
    WorkspaceMembership.objects.create(workspace=fleet.ws, user=other, role=WorkspaceMembership.EDITOR)
    vid = _ws_vault(fleet)
    c = Client()
    c.force_login(other)
    grant_step_up(c)
    assert c.post("/api/workspaces/connect/vaults", data={"name": "Mine"},
                  content_type="application/json").status_code == 403
    assert c.post(f"/api/vaults/{vid}/attachments", data={"agent": "eva"},
                  content_type="application/json").status_code == 403
    assert c.get(f"/api/vaults/{vid}").status_code == 200   # a member of the workspace may look


def test_a_keeper_grants_a_workspace_vault_too(fleet):
    assert _enable(fleet).status_code == 200
    vid, binding, vk = _ws_enabled(fleet, {"x": "y"})
    ed, ed_priv = _editor_with_a_key(fleet, name="ed3")   # full access to eva → attached user
    work = fleet.client.get(_keeper_url(fleet)).json()["agents"]
    row = next(w for w in work if w.get("vault_id") == vid)
    assert row["agent_id"] == binding and [(p["kind"], p["target"]) for p in row["pending"]] == [
        ("member", str(ed.pk))]
    wrap = ref.wrap_ecdh_es(vk, ed_priv.public_key(), ref.member_info(binding, 1, str(ed.pk)),
                            ec.generate_private_key(ec.SECP256R1()), os.urandom(12))
    res = fleet.client.post(row["grant_path"], data={"kv": 1, "wraps": [
        {"kind": "member", "target": str(ed.pk), "wrap": wrap}]}, content_type="application/json")
    assert res.status_code == 200 and res.json() == {"granted": 1}


def test_replacing_your_vault_key_puts_you_back_in_line_for_workspace_vaults_too(fleet):
    from apps.agents.credentials import workspace_vaults

    assert _enable(fleet).status_code == 200
    vid, _, _ = _ws_enabled(fleet, {"x": "y"})
    v = Vault.objects.get(pk=vid)
    assert VaultKeyWrap.objects.filter(vault=v, kind="member", user=fleet.user).exists()
    zk.forget_member_wraps(fleet.user)   # what PUT /api/vault/keyset {replace: true} runs
    assert not VaultKeyWrap.objects.filter(vault=v, kind="member", user=fleet.user).exists()
    assert ("member", str(fleet.user.pk)) in {(t["kind"], t["target"]) for t in workspace_vaults.pending_targets(v)}


def test_a_keeper_that_lost_the_race_adds_nothing(fleet):
    # Both keepers saw the target pending; the second's write finds a copy under
    # the lock and is refused, so exactly one grant stands.
    assert _enable(fleet).status_code == 200
    before = AgentVaultWrap.objects.get(agent=fleet.agent, kind="member", user=fleet.user).wrap
    with pytest.raises(zk.ZkError) as exc:
        zk.add_wraps(fleet.agent, kv=1, wraps=[ZkWrapIn(**member_wrap(fleet))], user=fleet.user, only_new=True)
    assert exc.value.code == "zk_not_pending"
    assert AgentVaultWrap.objects.get(agent=fleet.agent, kind="member", user=fleet.user).wrap == before


# ---- the keepers' shared record, and announcing a keeper's grant -------------------------

def _pins_value(fleet, kv=1, people=None) -> str:
    import json as _json

    return ref.encrypt_value(fleet.vk, _aid(fleet), "~keeper-pins", kv,
                             _json.dumps({"v": 1, "people": people or {}}), os.urandom(12))


def test_a_keeper_reads_and_writes_the_shared_record_over_the_version_it_read(fleet):
    assert _enable(fleet).status_code == 200
    _editor_with_a_key(fleet)
    [work] = fleet.client.get(_keeper_url(fleet)).json()["agents"]
    assert work["pins"] == {"value": "", "kv": 0, "version": 0}
    assert work["pins_path"] == _keeper_url(fleet, "/eva/pins")
    value = _pins_value(fleet)
    res = fleet.client.put(work["pins_path"], data={"kv": 1, "expected_version": 0, "value": value},
                           content_type="application/json")
    assert res.status_code == 200 and res.json() == {"value": value, "kv": 1, "version": 1}, res.content
    # A second keeper that read version 0 must read again and merge.
    res = fleet.client.put(work["pins_path"], data={"kv": 1, "expected_version": 0, "value": value},
                           content_type="application/json")
    assert res.status_code == 409 and res.json()["error"] == "keeper_pins_stale"
    [work] = fleet.client.get(_keeper_url(fleet)).json()["agents"]
    assert work["pins"]["version"] == 1


def test_only_a_holder_of_the_current_key_writes_the_record(fleet):
    assert _enable(fleet, wraps=[member_wrap(fleet), recovery_wrap(fleet)]).status_code == 200
    res = fleet.client.put(_keeper_url(fleet, "/eva/pins"), content_type="application/json",
                           data={"kv": 1, "expected_version": 0, "value": _pins_value(fleet)})
    assert res.status_code == 403 and res.json()["error"] == "zk_not_a_holder"


def test_a_keepers_grant_to_a_person_is_emailed_to_the_vaults_managers(fleet, settings):
    from django.core import mail

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    assert _enable(fleet).status_code == 200
    user, priv = _editor_with_a_key(fleet)
    mail.outbox.clear()
    res = fleet.client.post(_keeper_url(fleet, "/eva/wraps"), data={"kv": 1, "wraps": [
        member_wrap(fleet, user=user, pub=priv.public_key())]}, content_type="application/json")
    assert res.status_code == 200, res.content
    [msg] = [m for m in mail.outbox if "now has" in m.subject]
    assert msg.to == [fleet.user.email] and user.email in msg.subject


# ---- someone waiting for a vault's key: who is told, and what the vault page says ----------

def test_a_member_waiting_for_a_workspace_vault_is_emailed_to_its_holders(fleet, mailoutbox):
    from apps.agents.credentials import zk_notify
    from apps.agents.models import ZkGrantNotice

    assert _enable(fleet).status_code == 200
    vid, _, _ = _ws_enabled(fleet, {"x": "y"})
    ed, _ = _editor_with_a_key(fleet, name="ed5")   # full access to eva → a member of the attached vault
    t0 = timezone.now()
    zk_notify.sweep(t0)
    notice = ZkGrantNotice.objects.get(vault_id=vid, kind="member", target=str(ed.pk))
    mailoutbox.clear()
    assert zk_notify.sweep(t0 + zk_notify.GRACE) >= 1
    [msg] = [m for m in mailoutbox if "Shared" in m.subject]
    assert msg.to == ["jj@dimagi.com"] and "ed5@dimagi.com" in msg.subject
    assert f"/vaults/{vid}" in msg.body and "a new admin" not in msg.body
    assert notice.agent_id is None


def test_the_vault_page_says_who_is_waiting_and_who_can_pass_the_key_on(fleet):
    assert _enable(fleet).status_code == 200
    ed, _ = _editor_with_a_key(fleet, name="ed6")
    body = fleet.client.get("/api/agents/eva/vault/members").json()
    rows = {m["email"]: m for m in body["members"]}
    assert rows["jj@dimagi.com"]["has_copy"] is True
    assert rows["ed6@dimagi.com"]["has_vault_key"] is True and rows["ed6@dimagi.com"]["has_copy"] is False
    assert body["holders"] == {"has_key": True, "people": ["jj@dimagi.com"], "keepers": ["box"]}


# ---- a person who started over with a new key: the browser grant confirms it ---------------

def test_a_person_with_a_new_key_is_emailed_at_once_as_a_new_key(fleet, mailoutbox):
    from apps.agents.credentials import zk_notify

    assert _enable(fleet).status_code == 200
    ed, _ = _editor_with_a_key(fleet, name="ed7")
    UserVaultKeyset.objects.filter(user=ed).update(replaced_at=timezone.now())
    t0 = timezone.now()
    assert zk_notify.sweep(t0) >= 1           # no grace: no keeper grants a changed key
    [msg] = [m for m in mailoutbox if "ed7@dimagi.com" in m.subject]
    assert "new vault key" in msg.subject and "confirm it's them" in msg.subject
    assert "Check with ed7@dimagi.com directly" in msg.body
    assert "You're getting this as an admin of eva's vault" in msg.body


def test_someone_joining_for_the_first_time_is_named_as_that_to_the_admins(fleet, mailoutbox):
    from apps.agents.credentials import zk_notify

    assert _enable(fleet).status_code == 200
    _editor_with_a_key(fleet, name="ed8")
    t0 = timezone.now()
    assert zk_notify.sweep(t0) == 0            # a keeper usually grants a newcomer first
    assert zk_notify.sweep(t0 + zk_notify.GRACE) >= 1
    [msg] = [m for m in mailoutbox if "ed8@dimagi.com" in m.subject]
    assert "joining eva's vault for the first time" in msg.subject
    assert "If you expected ed8@dimagi.com to join" in msg.body
    assert "You're getting this as an admin of eva's vault" in msg.body


def test_a_member_who_holds_a_copy_but_cannot_grant_is_never_asked(fleet, mailoutbox):
    from apps.agents.credentials import zk_notify

    assert _enable(fleet, wraps=[member_wrap(fleet), recovery_wrap(fleet)]).status_code == 200
    ed, priv = _editor_with_a_key(fleet, name="ed9")
    assert fleet.client.post(f"{ZK}/wraps", data={"kv": 1, "wraps": [
        member_wrap(fleet, user=ed, pub=priv.public_key())]}, content_type="application/json").status_code == 200
    fleet.agent.refresh_from_db()
    assert zk_notify.holders(fleet.agent) == [fleet.user]
    body = fleet.client.get("/api/agents/eva/vault/members").json()
    assert body["holders"]["people"] == ["jj@dimagi.com"]   # ed9 holds a copy but can't let anyone in
    t0 = timezone.now()
    zk_notify.sweep(t0)
    assert zk_notify.sweep(t0 + zk_notify.GRACE) == 1        # the device, to jj only
    assert [m.to for m in mailoutbox] == [["jj@dimagi.com"]]
    assert "jj@dimagi.com's machine box" in mailoutbox[0].subject
    assert "You don't need to recognise the machine" in mailoutbox[0].body


def test_a_machine_whose_person_is_waiting_too_is_asked_about_as_the_person_only(fleet, mailoutbox):
    from apps.agents.credentials import zk_notify
    from apps.agents.models import ZkGrantNotice

    assert _enable(fleet).status_code == 200
    ed, _ = _editor_with_a_key(fleet, name="ed10")
    # Their own computer (no runner app), which reads eva's secrets because they administer it.
    AgentAdmin.objects.create(agent=fleet.agent, user=ed, granted_by=fleet.user)
    laptop = Runner.objects.create(name="DIM-SI-W021", kind=Runner.COMPUTER, owner=ed, status=Runner.ONLINE,
                                   workspace=fleet.ws, last_heartbeat_at=timezone.now(), capabilities={})
    # ed10 registers and approves their own laptop, as `canopy agent use` has them do.
    res = Client().post("/api/runner-device-keys/", content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {_pat(ed)}",
                        data={"runner": str(laptop.pk), "public_jwk": _jwk(ec.generate_private_key(ec.SECP256R1())),
                              "kind": "tpm", "label": "DIM-SI-W021 (Windows)",
                              "ecdh_public_jwk": ref.jwk(ec.generate_private_key(ec.SECP256R1()).public_key())})
    assert res.status_code in (200, 201), res.content
    key = RunnerDeviceKey.objects.get(pk=res.json()["id"])
    theirs = Client()
    theirs.force_login(ed)
    grant_step_up(theirs)
    assert theirs.post(f"/api/runner-device-keys/{key.pk}/approve").status_code == 200
    fleet.agent.refresh_from_db()
    pending = {(t["kind"], t["target"]) for t in zk.pending_targets(fleet.agent)}
    assert ("device", str(key.pk)) in pending and ("member", str(ed.pk)) in pending
    t0 = timezone.now()
    zk_notify.sweep(t0)
    zk_notify.sweep(t0 + zk_notify.GRACE)
    assert [m.subject for m in mailoutbox if "ed10@dimagi.com" in m.subject and "first time" in m.subject]
    assert not [m for m in mailoutbox if "DIM-SI-W021" in m.subject]
    notice = ZkGrantNotice.objects.get(agent=fleet.agent, kind="device", target=str(key.pk))
    assert notice.notified == {"folded_into_person": "ed10@dimagi.com"}


def test_replacing_your_key_records_when(fleet):
    ks = UserVaultKeyset.objects.get(user=fleet.user)
    assert ks.replaced_at is None
    zk.put_keyset(fleet.user, public_jwk=_jwk(ec.generate_private_key(ec.SECP256R1()).public_key()),
                  seals=ks.seals, replace=True)
    assert UserVaultKeyset.objects.get(user=fleet.user).replaced_at is not None


def test_an_owner_records_the_key_they_granted_to_from_the_browser(fleet):
    assert _enable(fleet).status_code == 200
    state = fleet.client.get(ZK).json()
    assert state["pins"] == {"value": "", "kv": 0, "version": 0}
    grant_step_up(fleet.client)
    res = fleet.client.put(f"{ZK}/pins", content_type="application/json",
                           data={"kv": 1, "expected_version": 0, "value": _pins_value(fleet)})
    assert res.status_code == 200 and res.json()["version"] == 1, res.content
    assert fleet.client.get(ZK).json()["pins"]["version"] == 1
    res = fleet.client.put(f"{ZK}/pins", content_type="application/json",
                           data={"kv": 1, "expected_version": 0, "value": _pins_value(fleet)})
    assert res.status_code == 409 and res.json()["error"] == "keeper_pins_stale"


def test_a_member_cannot_write_the_keepers_record(fleet):
    from django.test import Client

    assert _enable(fleet).status_code == 200
    ed, _ = _editor_with_a_key(fleet, name="ed8")
    c = Client()
    c.force_login(ed)
    grant_step_up(c)
    res = c.put(f"{ZK}/pins", content_type="application/json",
                data={"kv": 1, "expected_version": 0, "value": _pins_value(fleet)})
    assert res.status_code == 403


# ---- a workspace vault rotates as an agent's does (one engine: credentials/vault_engine.py) ----

def _ws_rotate(fleet, vid, binding, wraps, plain, *, new_kv=2, vk):
    values = [{"name": n, "value_zk": ref.encrypt_value(vk, binding, n, new_kv, v, os.urandom(12))}
              for n, v in plain.items()]
    return fleet.client.post(f"/api/vaults/{vid}/zk/rotate", content_type="application/json",
                             data={"new_kv": new_kv, "wraps": wraps, "values": values})


def test_a_workspace_vault_rotates_every_copy_and_value(fleet):
    assert _enable(fleet).status_code == 200
    plain = {"gog-oauth-client": "client-json"}
    vid, binding, _vk = _ws_enabled(fleet, plain)
    vk2 = os.urandom(32)
    res = _ws_rotate(fleet, vid, binding, _ws_wraps(fleet, binding, vk2, kv=2), plain, vk=vk2)
    assert res.status_code == 200, res.content
    assert res.json()["kv"] == 2
    assert set(VaultKeyWrap.objects.filter(vault_id=vid).values_list("kv", flat=True)) == {2}
    assert set(VaultSecret.objects.filter(vault_id=vid).values_list("kv", flat=True)) == {2}
    [layer] = _resolve(fleet).json()["vaults"]
    got = ref.unwrap_ecdh_es(layer["wrap"], fleet.ecdh, ref.device_info(binding, 2, fleet.device.ecdh_jkt))
    assert got == vk2
    assert ref.decrypt_value(vk2, binding, "gog-oauth-client", 2, layer["values"]["gog-oauth-client"]) == "client-json"


def test_a_workspace_vault_rotation_must_rewrap_everyone_still_eligible(fleet):
    assert _enable(fleet).status_code == 200
    plain = {"kindora": "shared-kin"}
    vid, binding, _vk = _ws_enabled(fleet, plain)
    vk2 = os.urandom(32)
    mem, dev, rec = _ws_wraps(fleet, binding, vk2, kv=2)
    res = _ws_rotate(fleet, vid, binding, [mem, rec], plain, vk=vk2)
    assert res.status_code == 422 and res.json()["error"] == "zk_rewrap_required"
    assert str(fleet.device.pk) in res.json()["detail"]
    res = _ws_rotate(fleet, vid, binding, _ws_wraps(fleet, binding, vk2, kv=3), plain, new_kv=3, vk=vk2)
    assert res.status_code == 409 and res.json()["error"] == "zk_kv_mismatch"
    res = _ws_rotate(fleet, vid, binding, [mem, dev, rec], {}, vk=vk2)
    assert res.status_code == 422 and res.json()["error"] == "zk_values_incomplete"


def test_a_workspace_vault_rotation_leaves_out_a_revoked_machine(fleet):
    assert _enable(fleet).status_code == 200
    plain = {"kindora": "shared-kin"}
    vid, binding, _vk = _ws_enabled(fleet, plain)
    fleet.client.delete(f"/api/runner-device-keys/{fleet.device.pk}")
    state = fleet.client.get(f"/api/vaults/{vid}/zk").json()
    assert state["rotation_recommended"] is True
    vk2 = os.urandom(32)
    mem, _dev, rec = _ws_wraps(fleet, binding, vk2, kv=2)
    res = _ws_rotate(fleet, vid, binding, [mem, rec], plain, vk=vk2)
    assert res.status_code == 200, res.content
    assert not VaultKeyWrap.objects.filter(vault_id=vid, kind="device").exists()
    assert res.json()["rotation_recommended"] is False


def test_a_browser_rotates_a_workspace_vault_only_after_a_passkey_step_up(fleet):
    assert _enable(fleet).status_code == 200
    plain = {"kindora": "shared-kin"}
    vid, binding, _vk = _ws_enabled(fleet, plain)
    c = Client()
    c.force_login(fleet.user)
    vk2 = os.urandom(32)
    res = c.post(f"/api/vaults/{vid}/zk/rotate", content_type="application/json",
                 data={"new_kv": 2, "wraps": _ws_wraps(fleet, binding, vk2, kv=2), "values": []})
    assert res.status_code == 403 and res.json()["error"] == "step_up_required"
