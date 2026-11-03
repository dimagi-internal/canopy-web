"""The connections wire format (plan Part 0 §0.1-0.4): grant statements and
writer-authenticated envelopes.

The NORMATIVE definition is docs/vault/zk-v1/connections_v1_reference.py with
its vectors beside it; the canopy CLI and the browser reproduce the same bytes.
These tests check canopy-web's own checks (`apps.connections.wire`) against the
vectors, and re-derive each MAC on the RECIPIENT's side with nothing but
`cryptography` — a path independent of the reference's writer-side code.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from apps.agents.credentials import zk_seal
from apps.connections import wire
from apps.harness import device_keys
from tests.fixtures.vault_zk import ref as zk_ref

ZK_DIR = Path(__file__).resolve().parents[1] / "docs" / "vault" / "zk-v1"
VECTORS = ZK_DIR / "connections_v1_vectors.json"


def _load_reference():
    spec = importlib.util.spec_from_file_location("connections_v1_reference", ZK_DIR / "connections_v1_reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ref = _load_reference()


def vectors() -> dict:
    return json.loads(VECTORS.read_text())


V = vectors()
GOOD = V["statement"]["statement"]
CID = uuid.UUID(GOOD["connection"])
SEALED = V["envelope"]["sealed"]
SIG = V["envelope"]["device_writer"]["sig"]


def _priv(scalar_hex: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int(scalar_hex, 16), ec.SECP256R1())


def _pub(jwk: dict) -> ec.EllipticCurvePublicKey:
    def raw(s):
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + raw(jwk["x"]) + raw(jwk["y"]))


def _entries(sealed: str) -> list[dict]:
    body = sealed[len("zks1:"):]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["r"]


# ---- the reference and its vectors ------------------------------------------------

def test_vectors_file_is_what_the_reference_generates():
    assert json.loads(VECTORS.read_text()) == ref.vectors()


def test_binding_id_is_the_lower_case_hyphenated_uuid():
    assert wire.binding_id(CID) == f"connection:{GOOD['connection']}"
    assert V["envelope"]["binding"] == wire.binding_id(CID)
    assert wire.binding_id(uuid.UUID(str(CID).upper())) == wire.binding_id(CID)


def test_vector_thumbprints_are_the_ones_canopy_web_computes():
    for r in V["envelope"]["recipients"]:
        if r["kind"] == "device":
            assert device_keys.thumbprint(r["public_jwk"]) == r["target"]
    assert device_keys.thumbprint(V["statement"]["signer_jwk"]) == V["statement"]["signer_jkt"]
    assert json.loads(V["envelope"]["plaintext"])["owner_key"] == V["statement"]["signer_jkt"]


# ---- grant statements ---------------------------------------------------------------

def test_statement_msg_matches_vector():
    v = vectors()["statement"]
    assert wire.statement_msg(v["statement"]) == v["msg"].encode()
    assert v["msg"].startswith('canopy-connections/v1/grant|{"account_id":')


def test_statement_msg_does_not_depend_on_key_order():
    shuffled = dict(reversed(list(GOOD.items())))
    assert wire.statement_msg(shuffled) == wire.statement_msg(GOOD)


def test_vector_signature_verifies_and_a_changed_agent_list_does_not():
    v = vectors()["statement"]
    assert wire.verify_statement(v["statement"], v["sig"], v["signer_jwk"])
    assert not wire.verify_statement({**v["statement"], "agents": ["12"]}, v["sig"], v["signer_jwk"])


def test_a_signature_by_another_key_or_of_the_wrong_shape_does_not_verify():
    v = vectors()["statement"]
    other = V["envelope"]["recipients"][0]["public_jwk"]
    assert not wire.verify_statement(v["statement"], v["sig"], other)
    assert not wire.verify_statement(v["statement"], v["sig"][:-4], v["signer_jwk"])
    assert not wire.verify_statement(v["statement"], "not base64!", v["signer_jwk"])
    assert not wire.verify_statement(v["statement"], v["sig"], {"kty": "EC", "x": "AA", "y": "AA"})


def test_a_signature_made_like_the_browser_endorse_verifies():
    # A fresh randomized ECDSA signature, P1363 r||s as WebCrypto returns it: not the
    # vector's deterministic one, so verification cannot be byte-comparing.
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    signer = _priv(V["statement"]["signer_priv_scalar_hex"])
    r, s = decode_dss_signature(signer.sign(V["statement"]["msg"].encode(), ec.ECDSA(hashes.SHA256())))
    sig = base64.urlsafe_b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).rstrip(b"=").decode()
    assert wire.verify_statement(GOOD, sig, V["statement"]["signer_jwk"])


def test_good_statement_is_accepted():
    assert wire.check_statement(dict(GOOD), connection_id=CID, account_id=GOOD["account_id"]) == GOOD


def test_an_agent_owned_statement_with_no_computers_is_accepted():
    st = {**GOOD, "my_computers": []}
    assert wire.check_statement(st, connection_id=CID, account_id=GOOD["account_id"]) == st


@pytest.mark.parametrize("bad", [{"agents": ["40", "12"]}, {"agents": ["12", "12"]}, {"version": 0},
                                 {"account_id": 'a"b'}, {"v": 2}, {"extra": 1}])
def test_malformed_statement_is_refused(bad):
    with pytest.raises(wire.WireError) as e:
        wire.check_statement({**GOOD, **bad}, connection_id=CID, account_id=GOOD["account_id"])
    assert (e.value.status, e.value.code) == (422, "bad_statement")


@pytest.mark.parametrize("bad", [
    {"agents": ["12", "4a"]}, {"agents": ["012"]}, {"agents": [12]}, {"agents": "12"},
    {"my_computers": ["b", "a"]}, {"my_computers": ["short"]}, {"my_computers": [GOOD["my_computers"][0]] * 2},
    {"version": True}, {"version": "3"}, {"version": -1}, {"v": True},
    {"account_id": "a\\b"}, {"account_id": "a b"}, {"account_id": ""}, {"account_id": "é"},
    {"account_id": "x" * 256}, {"connection": str(CID).upper()},
    # A trailing newline is another spelling of the same value: never accepted.
    {"agents": ["12", "12\n"]}, {"agents": ["12\n", "40"]}, {"my_computers": [GOOD["my_computers"][0] + "\n"]},
    {"account_id": GOOD["account_id"] + "\n"}, {"connection": str(CID) + "\n"},
])
def test_other_malformed_statements_are_refused(bad):
    with pytest.raises(wire.WireError) as e:
        wire.check_statement({**GOOD, **bad}, connection_id=CID, account_id=bad.get("account_id", GOOD["account_id"]))
    assert (e.value.status, e.value.code) == (422, "bad_statement")


def _jkts(n: int) -> list[str]:
    """`n` well-formed thumbprints, sorted."""
    return sorted(base64.urlsafe_b64encode(hashlib.sha256(str(i).encode()).digest()).rstrip(b"=").decode()
                  for i in range(n))


def test_the_vector_statement_names_who_may_vouch():
    assert set(GOOD) == {"account_id", "agents", "connection", "endorsers", "my_computers", "v", "version"}
    assert GOOD["endorsers"] and GOOD["endorsers"] == sorted(set(GOOD["endorsers"]))
    v = vectors()["statement"]
    # Signed: a server that adds an endorser breaks the signature.
    assert not wire.verify_statement({**v["statement"], "endorsers": []}, v["sig"], v["signer_jwk"])
    assert not wire.verify_statement({**v["statement"], "endorsers": [*_jkts(1), *GOOD["endorsers"]]}, v["sig"],
                                     v["signer_jwk"])


@pytest.mark.parametrize("endorsers", [[], _jkts(1), _jkts(64)])
def test_a_statement_may_list_up_to_64_endorsers_or_none(endorsers):
    st = {**GOOD, "endorsers": endorsers}
    assert wire.check_statement(st, connection_id=CID, account_id=GOOD["account_id"]) == st


@pytest.mark.parametrize("endorsers", [
    _jkts(65), list(reversed(_jkts(2))), [_jkts(1)[0]] * 2, ["short"], [_jkts(1)[0] + "\n"], [1], "x", None,
])
def test_malformed_endorsers_are_refused(endorsers):
    with pytest.raises(wire.WireError) as e:
        wire.check_statement({**GOOD, "endorsers": endorsers}, connection_id=CID, account_id=GOOD["account_id"])
    assert (e.value.status, e.value.code) == (422, "bad_statement")


def test_a_statement_missing_a_member_or_not_an_object_is_refused():
    for st in ({k: v for k, v in GOOD.items() if k != "my_computers"},
               {k: v for k, v in GOOD.items() if k != "endorsers"}, [], "statement", None):
        with pytest.raises(wire.WireError) as e:
            wire.check_statement(st, connection_id=CID, account_id=GOOD["account_id"])
        assert (e.value.status, e.value.code) == (422, "bad_statement")


def test_statement_for_another_account_or_connection_is_refused():
    for kwargs in ({"connection_id": uuid.uuid4(), "account_id": GOOD["account_id"]},
                   {"connection_id": CID, "account_id": "00D000000000002AAA/005000000000001AAA"}):
        with pytest.raises(wire.WireError) as e:
            wire.check_statement(dict(GOOD), **kwargs)
        assert (e.value.status, e.value.code) == (422, "bad_statement")


# ---- writer-authenticated envelopes -------------------------------------------------

def _mac_on_the_recipient_side(binding, name, expect, entry, recipient_priv, writer_jwk) -> str:
    """The §0.4 MAC re-derived from the recipient's end, straight from the contract text."""
    digest = hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    msg = f"canopy-connections/v1/envelope|{binding}|{name}|{expect}|{digest}"
    z = recipient_priv.exchange(ec.ECDH(), _pub(writer_jwk))
    info = f"canopy-connections/v1/envelope-mac|{binding}|{name}|{entry['kind']}:{entry['target']}"
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"", info=info.encode()).derive(z)
    return base64.urlsafe_b64encode(hmac.new(key, msg.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()


@pytest.mark.parametrize("writer", ["device_writer", "member_writer"])
def test_envelope_mac_vector_recomputes_for_each_recipient(writer):
    env = V["envelope"]
    w = env[writer]
    sig = w["sig"]
    privs = {(r["kind"], r["target"]): _priv(r["priv_scalar_hex"]) for r in env["recipients"]}
    macs = {(m["kind"], m["target"]): m["mac"] for m in sig["macs"]}
    entries = _entries(env["sealed"])
    assert {(e["kind"], e["target"]) for e in entries} == set(privs) == set(macs)
    writer_pub = _pub(sig["by"]["public_jwk"])
    for e in entries:
        who = (e["kind"], e["target"])
        assert ref.verify_envelope_mac(env["binding"], env["name"], sig["expect"], e, macs[who],
                                       privs[who], writer_pub)
        assert macs[who] == _mac_on_the_recipient_side(env["binding"], env["name"], sig["expect"], e,
                                                       privs[who], sig["by"]["public_jwk"])
        # Bound to expect, name, binding and the entry itself.
        for b, n, x, ent in ((env["binding"], env["name"], "0" * 64, e),
                             (env["binding"], "access", sig["expect"], e),
                             (wire.binding_id(uuid.uuid4()), env["name"], sig["expect"], e),
                             (env["binding"], env["name"], sig["expect"], {**e, "ct": e["ct"][:-2] + "AA"})):
            assert not ref.verify_envelope_mac(b, n, x, ent, macs[who], privs[who], writer_pub)


def test_envelope_vector_opens_to_the_refresh_plaintext_for_every_recipient():
    env = V["envelope"]
    assert zk_seal.check_sealed_value(env["sealed"], "sealed") == env["sealed"]
    assert hashlib.sha256(env["sealed"].encode()).hexdigest() == env["sealed_digest"]
    for r in env["recipients"]:
        opened = zk_ref.open_sealed_value(env["sealed"], env["binding"], env["name"], r["kind"], r["target"],
                                          _priv(r["priv_scalar_hex"]))
        assert opened == env["plaintext"]
    pt = json.loads(env["plaintext"])
    assert set(pt) == {"v", "provider", "refresh_token", "client_id", "login_host", "instance_url",
                       "account_id", "owner_key", "issued_at", "scopes", "statement_floor"}
    assert pt["account_id"] == GOOD["account_id"]
    # The share that wrote this envelope wrote its own statement's version.
    assert pt["statement_floor"] == GOOD["version"]


def test_the_access_plaintext_names_the_same_owner_key_as_the_refresh_one():
    access, refresh = json.loads(V["envelope"]["access_plaintext"]), json.loads(V["envelope"]["plaintext"])
    assert set(access) == {"v", "access_token", "expires_at", "instance_url", "account_id", "scope", "owner_key",
                           "statement_floor"}
    assert access["statement_floor"] == refresh["statement_floor"] == GOOD["version"]
    assert access["owner_key"] == refresh["owner_key"] == V["statement"]["signer_jkt"]
    assert access["account_id"] == refresh["account_id"]


@pytest.mark.parametrize("writer", ["device_writer", "member_writer"])
def test_vector_writer_sigs_pass_the_structure_check(writer):
    sig = V["envelope"][writer]["sig"]
    assert wire.check_writer_sig(SEALED, sig) == sig


def test_writer_sig_must_cover_exactly_the_sealed_recipients():
    with pytest.raises(wire.WireError) as e:
        wire.check_writer_sig(SEALED, {**SIG, "macs": SIG["macs"][:1]})
    assert e.value.code == "bad_writer_sig"


def _bad_sigs():
    m0 = SIG["macs"][0]
    by = SIG["by"]
    return {
        "extra mac": {**SIG, "macs": [*SIG["macs"], {"kind": "device", "target": "x" * 43, "mac": m0["mac"]}]},
        "duplicate mac": {**SIG, "macs": [*SIG["macs"], m0]},
        "short mac": {**SIG, "macs": [{**m0, "mac": m0["mac"][:20]}, *SIG["macs"][1:]]},
        "mac not b64u": {**SIG, "macs": [{**m0, "mac": "!" * 43}, *SIG["macs"][1:]]},
        "mac extra key": {**SIG, "macs": [{**m0, "x": 1}, *SIG["macs"][1:]]},
        "macs not a list": {**SIG, "macs": {}},
        "by kind": {**SIG, "by": {**by, "kind": "agent"}},
        "device target not its thumbprint": {**SIG, "by": {**by, "target": SIG["macs"][0]["target"]
                                                           if SIG["macs"][0]["target"] != by["target"]
                                                           else SIG["macs"][1]["target"]}},
        "by not a key": {**SIG, "by": {**by, "public_jwk": {"kty": "EC", "crv": "P-256", "x": "AA", "y": "AA"}}},
        "by jwk extra member": {**SIG, "by": {**by, "public_jwk": {**by["public_jwk"], "d": "AA"}}},
        "member target not an id": {**SIG, "by": {**by, "kind": "member", "target": "seven"}},
        "expect not a digest": {**SIG, "expect": "abc"},
        "expect upper hex": {**SIG, "expect": "A" * 64},
        "expect trailing newline": {**SIG, "expect": SIG["expect"] + "\n"},
        "member target trailing newline": {**SIG, "by": {**by, "kind": "member", "target": "7\n"}},
        "mac trailing newline": {**SIG, "macs": [{**m0, "mac": m0["mac"] + "\n"}, *SIG["macs"][1:]]},
        "extra member": {**SIG, "extra": 1},
        "missing by": {k: v for k, v in SIG.items() if k != "by"},
        "not an object": [],
    }


@pytest.mark.parametrize("case", list(_bad_sigs()))
def test_a_malformed_writer_sig_is_refused(case):
    with pytest.raises(wire.WireError) as e:
        wire.check_writer_sig(SEALED, _bad_sigs()[case])
    assert (e.value.status, e.value.code) == (422, "bad_writer_sig")


def test_a_writer_sig_on_something_that_is_not_a_sealed_value_is_refused():
    for value in ("", "zk1:AAAA", "zks1:not-json"):
        with pytest.raises(wire.WireError) as e:
            wire.check_writer_sig(value, SIG)
        assert e.value.code == "bad_writer_sig"


def test_an_empty_expect_is_a_first_write():
    assert wire.check_writer_sig(SEALED, {**SIG, "expect": ""})["expect"] == ""


def test_keep_only_sig_drops_pruned_macs():
    keep = {(m["kind"], m["target"]) for m in SIG["macs"][1:]}
    pruned = wire.keep_only_sig(SIG, keep)
    assert {(m["kind"], m["target"]) for m in pruned["macs"]} == keep
    assert pruned["by"] == SIG["by"] and pruned["expect"] == SIG["expect"]
    assert len(SIG["macs"]) == 3, "the input is not mutated"
    # Pruning the envelope and its sig together leaves a sig that still covers it exactly,
    # and every surviving MAC still verifies (each MAC covers only its own entry).
    value, dropped = zk_seal.keep_only(SEALED, keep)
    assert dropped == {(SIG["macs"][0]["kind"], SIG["macs"][0]["target"])}
    assert wire.check_writer_sig(value, pruned) == pruned
    privs = {(r["kind"], r["target"]): _priv(r["priv_scalar_hex"]) for r in V["envelope"]["recipients"]}
    macs = {(m["kind"], m["target"]): m["mac"] for m in pruned["macs"]}
    for e in _entries(value):
        who = (e["kind"], e["target"])
        assert ref.verify_envelope_mac(V["envelope"]["binding"], "refresh", SIG["expect"], e, macs[who],
                                       privs[who], _pub(SIG["by"]["public_jwk"]))


def test_keep_only_sig_leaves_no_sig_as_none():
    assert wire.keep_only_sig(None, set()) is None


def test_names_and_prefix():
    assert (wire.REFRESH_NAME, wire.ACCESS_NAME) == ("refresh", "access")
    assert wire.STATEMENT_PREFIX == "canopy-connections/v1/grant|"
