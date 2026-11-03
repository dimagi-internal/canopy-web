"""canopy-web's own KMS key as a sealed-connection recipient (apps/connections/server_key.py),
`zk_seal.open_for`, and the stack that creates the key."""
from __future__ import annotations

import json
from unittest import mock

import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric import ec

from apps.agents.credentials import zk, zk_seal
from apps.connections import server_key
from tests.fixtures.vault_zk import VECTORS, ref
from tests.test_task_role_stack import _load

AGENT = "conn-binding-1"


@pytest.fixture
def key():
    k = ec.generate_private_key(ec.SECP256R1())
    server_key.use_software_key_for_tests(k)
    yield k
    server_key.use_software_key_for_tests(None)


def _jwk(priv):
    return ref.jwk(priv.public_key())


def _seal(priv, plain="refresh-1", *, kv=3, kind="device", target=None):
    target = target or server_key.thumbprint()
    return zk_seal.seal_to(plain, agent_id=AGENT, name="refresh", kv=kv,
                           recipients=[(kind, target, _jwk(priv))])


def _open(value, **over):
    args = dict(agent_id=AGENT, name="refresh", kv=3, kind="device", target=server_key.thumbprint(),
                derive=server_key.derive)
    return zk_seal.open_for(value, **(args | over))


def test_open_for_opens_a_value_seal_to_made(key):
    assert _open(_seal(key)) == "refresh-1"


def test_open_for_reproduces_the_zk_v1_vector():
    i = VECTORS["inputs"]
    priv = ec.derive_private_key(int(i["device_priv_scalar_hex"], 16), ec.SECP256R1())
    out = zk_seal.open_for(VECTORS["outputs"]["sealed_value"], agent_id=i["agent_id"], name=i["sealed_name"],
                           kv=1, kind="device", target=i["device_jkt"],
                           derive=lambda epk: priv.exchange(ec.ECDH(), zk.public_key(epk)))
    assert out == i["sealed_plaintext"]


@pytest.mark.parametrize("over", [{"name": "access"}, {"kv": 4}, {"agent_id": "other"},
                                  {"kind": "member"}, {"target": "someone-else"}])
def test_a_wrong_binding_or_missing_entry_raises_zkerror(key, over):
    with pytest.raises(zk.ZkError):
        _open(_seal(key), **over)


def test_a_tampered_entry_raises_zkerror(key):
    v = _seal(key)
    body = json.loads(zk.b64u_bytes(v[len("zks1:"):], "v"))
    ct = bytearray(zk.b64u_bytes(body["r"][0]["ct"], "ct"))
    ct[-1] ^= 1
    body["r"][0]["ct"] = zk_seal._b64u(bytes(ct))
    bad = "zks1:" + zk_seal._b64u(json.dumps(body).encode())
    with pytest.raises(zk.ZkError):
        _open(bad)


def test_a_value_that_is_not_sealed_raises_zkerror(key):
    with pytest.raises(zk.ZkError):
        _open("zk1:abc")


def test_thumbprint_and_public_jwk_are_the_keys(key):
    assert server_key.public_jwk() == _jwk(key)
    assert server_key.thumbprint() == ref.jkt(key.public_key())


def test_available_is_false_when_unset_and_derive_refuses(settings):
    settings.CONNECTIONS_SERVER_KMS_KEY = ""
    assert server_key.available() is False
    with pytest.raises(server_key.Unavailable):
        server_key.derive(_jwk(ec.generate_private_key(ec.SECP256R1())))


def test_available_when_a_key_is_configured(settings):
    settings.CONNECTIONS_SERVER_KMS_KEY = "alias/x"
    assert server_key.available() is True


def test_software_key_hook_refuses_outside_tests(settings):
    settings.TESTING = False
    with pytest.raises(RuntimeError):
        server_key.use_software_key_for_tests(ec.generate_private_key(ec.SECP256R1()))


def _kms(settings, **methods):
    settings.CONNECTIONS_SERVER_KMS_KEY = "alias/canopy-web-connections"
    server_key._reset()
    client = mock.Mock(**methods)
    return mock.patch.object(server_key, "_client", return_value=client), client


def test_derive_calls_kms_with_the_peer_as_der_and_returns_the_secret(settings):
    peer = ec.generate_private_key(ec.SECP256R1())
    ctx, client = _kms(settings, derive_shared_secret=mock.Mock(return_value={"SharedSecret": b"z" * 32}))
    with ctx:
        assert server_key.derive(_jwk(peer)) == b"z" * 32
    kw = client.derive_shared_secret.call_args.kwargs
    assert kw["KeyId"] == "alias/canopy-web-connections" and kw["KeyAgreementAlgorithm"] == "ECDH"
    from cryptography.hazmat.primitives import serialization
    assert kw["PublicKey"] == peer.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def test_a_boto_client_error_is_unavailable_with_no_secret_in_it(settings):
    err = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "SECRET-DETAIL"}}, "DeriveSharedSecret")
    ctx, _ = _kms(settings, derive_shared_secret=mock.Mock(side_effect=err))
    with ctx, pytest.raises(server_key.Unavailable) as e:
        server_key.derive(_jwk(ec.generate_private_key(ec.SECP256R1())))
    assert "SECRET-DETAIL" not in str(e.value) and e.value.__cause__ is None


def test_get_public_key_failure_is_unavailable(settings):
    err = ClientError({"Error": {"Code": "NotFoundException", "Message": "m"}}, "GetPublicKey")
    ctx, _ = _kms(settings, get_public_key=mock.Mock(side_effect=err))
    with ctx, pytest.raises(server_key.Unavailable):
        server_key.public_jwk()


def test_a_key_of_the_wrong_kind_is_unavailable(settings):
    ctx, _ = _kms(settings, get_public_key=mock.Mock(return_value={
        "KeySpec": "RSA_2048", "KeyUsage": "ENCRYPT_DECRYPT", "PublicKey": b"x"}))
    with ctx, pytest.raises(server_key.Unavailable):
        server_key.public_jwk()


def test_public_key_from_kms_is_cached_per_process(settings):
    from cryptography.hazmat.primitives import serialization
    k = ec.generate_private_key(ec.SECP256R1())
    der = k.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ctx, client = _kms(settings, get_public_key=mock.Mock(return_value={
        "KeySpec": "ECC_NIST_P256", "KeyUsage": "KEY_AGREEMENT", "PublicKey": der}))
    with ctx:
        assert server_key.public_jwk() == _jwk(k)
        server_key.thumbprint()
    assert client.get_public_key.call_count == 1


# --- the stack ---------------------------------------------------------------------------
def test_the_key_stack_is_a_p256_key_agreement_key_only_the_task_role_derives_with():
    t = _load("canopy-web-connections-key.cfn.yaml")
    key = next(r for r in t["Resources"].values() if r["Type"] == "AWS::KMS::Key")
    p = key["Properties"]
    assert p["KeySpec"] == "ECC_NIST_P256" and p["KeyUsage"] == "KEY_AGREEMENT"
    assert key["DeletionPolicy"] == "Retain"
    st = {s["Sid"]: s for s in p["KeyPolicy"]["Statement"]}
    use = next(s for s in st.values() if s["Principal"]["AWS"] == {"Ref": "TaskRoleArn"})
    assert set(use["Action"]) == {"kms:DeriveSharedSecret", "kms:GetPublicKey"}
    for s in st.values():
        if s is not use:
            assert "kms:DeriveSharedSecret" not in s["Action"]
