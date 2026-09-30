"""The contract's constants and pure helpers — the values both sides must compute
byte-for-byte identically."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

import canopy_sdk
from canopy_sdk import contract
from canopy_sdk.contract import ContractError
from canopy_sdk.jose import make_dpop_proof, unverified_header, verify_dpop_proof
from canopy_sdk.keys import generate_private_key, header_jwk, load_private_key, public_jwk, public_key_for, select_key

ROOT = Path(__file__).resolve().parent.parent


def test_the_version_is_one_value():
    if sys.version_info < (3, 11):
        pytest.skip("tomllib is 3.11+")
    import tomllib

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert canopy_sdk.__version__ == declared, "keep canopy_sdk.__version__ equal to pyproject's version"
    assert canopy_sdk.CONTRACT_VERSION == contract.CONTRACT_VERSION == "1"


def test_the_lifetime_caps_are_the_contracts():
    assert contract.ASSERTION_MAX_LIFETIME == 120
    assert contract.ID_JAG_MAX_LIFETIME == 300
    assert contract.CLIENT_ASSERTION_MAX_LIFETIME == 60
    assert contract.ACCESS_TOKEN_MAX_LIFETIME == 900
    assert contract.DPOP_IAT_WINDOW_SECONDS == 60


def test_only_asymmetric_algorithms_anywhere():
    assert set(contract.GRANT_ALGORITHMS) == {"EdDSA", "ES256"}
    assert set(contract.ASSERTION_ALGORITHMS) == {"EdDSA", "ES256", "RS256"}
    for alg in contract.ASSERTION_ALGORITHMS:
        assert not alg.startswith("HS") and alg != "none"


def test_the_thumbprint_is_rfc_7638_rfc_8037_appendix_a3():
    jwk = {"crv": "Ed25519", "kty": "OKP", "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}
    assert contract.jwk_thumbprint(jwk) == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"
    assert contract.jwk_thumbprint({**jwk, "kid": "x", "use": "sig"}) == contract.jwk_thumbprint(jwk)


@pytest.mark.parametrize("jwk", [{"kty": "RSA", "n": "x", "e": "AQAB"}, {"kty": "OKP"}, {}, "nope"])
def test_a_thumbprint_of_an_unusable_key_is_refused(jwk):
    with pytest.raises(ContractError):
        contract.jwk_thumbprint(jwk)


def test_ath_is_rfc_9449s_example():
    assert contract.ath("Kz~8mXK1EalYznwH-LC-1fBAo.4Ljp~zsPE_NeO.gxU") == \
        "fUHyO2r2Z3DZ53EsNrWBb0xWXoaNy59IiKCAqksmQEo"


def test_metadata_urls_follow_the_well_known_insertion_rule():
    assert contract.metadata_url("https://host.test") == "https://host.test/.well-known/oauth-authorization-server"
    assert contract.metadata_url("https://host.test/tenant/") == \
        "https://host.test/.well-known/oauth-authorization-server/tenant"
    assert contract.protected_resource_metadata_url("https://host.test/mcp/") == \
        "https://host.test/.well-known/oauth-protected-resource/mcp"


def test_htu_normalisation():
    assert contract.normalize_htu("HTTPS://Host.Test/mcp/?q=1#f") == "https://host.test/mcp"


def test_constant_time_equal_refuses_non_strings():
    assert contract.constant_time_equal("a", "a")
    assert not contract.constant_time_equal(None, None)
    assert not contract.constant_time_equal(1, 1)
    assert not contract.constant_time_equal("a", b"a")


def test_the_client_metadata_document_shape():
    doc = contract.client_metadata_document("https://c/oauth/client.json", "https://c/oauth/jwks.json")
    assert tuple(doc) == contract.CLIENT_METADATA_FIELDS
    assert doc["grant_types"] == [contract.JWT_BEARER_GRANT] and doc["dpop_bound_access_tokens"] is True
    assert contract.client_id_for("https://c/") == "https://c/oauth/client.json"


def test_lifetime_and_jti_checks():
    contract.check_lifetime({"iat": 0, "exp": 60}, 60)
    with pytest.raises(ContractError):
        contract.check_lifetime({"iat": 0, "exp": 61}, 60)
    with pytest.raises(ContractError):
        contract.check_jti({"jti": "x" * 257})
    with pytest.raises(ContractError):
        contract.check_jti({})
    with pytest.raises(ContractError):
        contract.check_dpop_iat(True)


# --- keys ----------------------------------------------------------------------------


def test_a_published_jwk_carries_no_private_member_and_a_thumbprint_kid():
    for alg in ("EdDSA", "ES256"):
        jwk = public_jwk(generate_private_key(alg))
        assert "d" not in jwk and jwk["kid"] == contract.jwk_thumbprint(jwk) and jwk["alg"] == alg


def test_public_key_for_checks_the_shape_against_the_algorithm():
    okp = public_jwk(ed25519.Ed25519PrivateKey.generate())
    with pytest.raises(ContractError):
        public_key_for(okp, "ES256")
    with pytest.raises(ContractError):
        public_key_for({**okp, "d": "x"}, "EdDSA")
    with pytest.raises(ContractError):
        public_key_for(okp, "RS256")
    with pytest.raises(ContractError):
        public_key_for({**okp, "alg": "ES256"}, "EdDSA")
    assert public_key_for(okp, "EdDSA")


def test_select_key_is_unambiguous_or_nothing():
    a, b = public_jwk(generate_private_key()), public_jwk(generate_private_key())
    assert select_key([a], None) == a
    assert select_key([a, b], None) is None
    assert select_key([a, b], b["kid"]) == b
    assert select_key([{**a, "use": "enc"}], a["kid"]) is None


def test_load_private_key_refuses_rsa():
    from cryptography.hazmat.primitives.asymmetric import rsa

    with pytest.raises(ContractError):
        load_private_key(rsa.generate_private_key(public_exponent=65537, key_size=2048))


# --- DPoP ------------------------------------------------------------------------------


def test_a_made_proof_verifies_and_binds_the_token():
    key = ec.generate_private_key(ec.SECP256R1())
    proof = make_dpop_proof(key, "post", "https://host.test/mcp/", access_token="tok", nonce="n")
    header = jwt.get_unverified_header(proof)
    assert header["typ"] == "dpop+jwt" and "d" not in header["jwk"] and header["jwk"] == header_jwk(key)
    jkt, jti, iat = verify_dpop_proof(proof, htm="POST", htu="https://host.test/mcp", access_token="tok")
    assert jkt == public_jwk(key)["kid"] and jti and abs(iat - time.time()) < 5
    assert jwt.decode(proof, options={"verify_signature": False})["nonce"] == "n"


def test_a_proof_with_a_mismatched_key_shape_is_refused():
    key = ed25519.Ed25519PrivateKey.generate()
    proof = jwt.encode({"htm": "POST", "htu": "https://h/", "iat": int(time.time()), "jti": "j"}, key,
                       algorithm="EdDSA", headers={"typ": "dpop+jwt", "jwk": {"kty": "EC", "crv": "P-256",
                                                                              "x": "a", "y": "b"}})
    with pytest.raises(ContractError):
        verify_dpop_proof(proof, htm="POST", htu="https://h/")


def test_a_non_ascii_access_token_is_a_refusal_not_a_crash():
    key = ec.generate_private_key(ec.SECP256R1())
    proof = make_dpop_proof(key, "POST", "https://h/", access_token="tok")
    with pytest.raises(ContractError):
        verify_dpop_proof(proof, htm="POST", htu="https://h/", access_token="tók")


def test_an_oversized_or_unsigned_token_is_refused_before_any_check():
    with pytest.raises(ContractError):
        unverified_header("x" * (contract.MAX_JWT_BYTES + 1))
    unsigned = jwt.encode({"a": 1}, None, algorithm="none")
    with pytest.raises(ContractError):
        unverified_header(unsigned)


def test_the_only_flag_today_is_zdr():
    assert contract.RUNNER_FLAGS == frozenset({"zdr"})


@pytest.mark.parametrize("value,expected", [
    (None, ()), ([], ()), (["zdr"], ("zdr",)), (["zdr", "zdr"], ("zdr",)),
])
def test_parse_runner_requirements_normalises(value, expected):
    assert contract.parse_runner_requirements(value) == expected


@pytest.mark.parametrize("value", ["zdr", ["ZDR"], ["nope"], [1], {"zdr": True}])
def test_parse_runner_requirements_refuses_anything_else(value):
    with pytest.raises(ValueError):
        contract.parse_runner_requirements(value)
