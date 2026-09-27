"""The host's jwt-bearer grant (``GrantHandler``).

Ported from connect-labs ``connect_labs/mcp/tests/test_delegation.py`` (PR
#2060): every refusal path there has a counterpart here. Most of this file is
refusals, because that is where the security lives — each request fails exactly
one check and nothing else.
"""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from canopy_sdk import contract
from canopy_sdk.host import GrantRefused, HostConfig, MetadataError, issue_id_jag, sign_visitor_assertion
from canopy_sdk.host.grant import GrantHandler
from canopy_sdk.keys import private_pem
from canopy_sdk.stores import MemoryJtiStore, MemoryTokenStore

from .helpers import CLIENT_ID, ISSUER, RESOURCE, TOKEN_ENDPOINT, World, ec_jwk


@pytest.fixture
def world():
    return World()


def refused(world, **kwargs) -> GrantRefused:
    with pytest.raises(GrantRefused) as info:
        world.redeem(**kwargs)
    return info.value


# --- success ----------------------------------------------------------------------


def test_a_valid_grant_issues_a_short_dpop_bound_token_with_no_refresh(world):
    result = world.redeem()
    body = result.body()

    assert body["token_type"] == "DPoP"
    assert 0 < body["expires_in"] <= 900
    assert body["scope"] == "marketplace:read"
    assert "refresh_token" not in body
    assert result.headers()["Cache-Control"] == "no-store"

    stored = world.token_store.get(contract.token_checksum(body["access_token"]))
    assert stored.subject == "42"
    assert stored.client_id == CLIENT_ID and stored.actor == CLIENT_ID
    assert stored.cnf_jkt == contract.jwk_thumbprint(ec_jwk(world.client.dpop_key))
    assert body["access_token"] not in stored.token_checksum, "only the hash is stored"


def test_the_sdk_issued_id_jag_is_accepted(world):
    """``issue_id_jag`` and ``GrantHandler`` are two halves of one contract."""
    result = world.redeem(assertion=issue_id_jag(world.config, "42", ["marketplace:read"]))
    assert result.token.scopes == ("marketplace:read",)


def test_a_requested_scope_may_narrow_but_not_widen(world):
    widened = refused(world, extra={"scope": "marketplace:read mcp"})
    assert widened.code == "invalid_scope" and widened.status == 400
    assert world.redeem(extra={"scope": "marketplace:read"}).token.scopes == ("marketplace:read",)


def test_an_empty_requested_scope_is_refused(world):
    assert refused(world, extra={"scope": ""}).code == "invalid_scope"


# --- the client ---------------------------------------------------------------------


def test_the_grant_is_off_until_a_canopy_client_is_configured(world):
    off = HostConfig(signing_key=private_pem(world.host_key), issuer=ISSUER, resource=RESOURCE,
                     token_endpoint=TOKEN_ENDPOINT, canopy_client_id="",
                     scope_tools={"marketplace:read": ["x"]})
    handler = GrantHandler(off, jti_store=MemoryJtiStore(), token_store=MemoryTokenStore(),
                           client_keys=world.resolver)
    with pytest.raises(GrantRefused) as info:
        handler.handle(world.form(), world.proof())
    assert info.value.code == "unsupported_grant_type"
    assert len(world.token_store) == 0


def test_only_the_jwt_bearer_grant_is_handled(world):
    assert refused(world, extra={"grant_type": "client_credentials"}).code == "unsupported_grant_type"


def test_only_the_configured_client_may_redeem(world):
    r = refused(world, extra={"client_id": "https://evil.example/client.json"})
    assert (r.code, r.status, r.reason) == ("invalid_client", 401, "unknown_client")


@pytest.mark.parametrize("drop", ["client_assertion", "client_assertion_type"])
def test_the_client_must_authenticate(world, drop):
    r = refused(world, drop=(drop,))
    assert (r.code, r.status) == ("invalid_client", 401)


def test_a_client_assertion_by_an_unpublished_key_is_refused(world):
    forged = jwt.encode({"iss": CLIENT_ID, "sub": CLIENT_ID, "aud": ISSUER, "iat": int(time.time()),
                         "exp": int(time.time()) + 60, "jti": "x1"},
                        ed25519.Ed25519PrivateKey.generate(), algorithm="EdDSA",
                        headers={"kid": world.client_jwk["kid"]})
    r = refused(world, client_assertion=forged)
    assert (r.code, r.status) == ("invalid_client", 401)


def test_a_client_assertion_naming_an_unknown_kid_is_refused_after_one_refetch(world):
    forged = world.client_assertion()
    header_kid_forged = jwt.encode(jwt.decode(forged, options={"verify_signature": False}),
                                   world.client.client_key, algorithm="EdDSA", headers={"kid": "nope"})
    assert refused(world, client_assertion=header_kid_forged).reason == "client_unknown_key"
    assert refused(world, client_assertion=header_kid_forged).reason == "client_unknown_key"
    # One cached read + one early refetch, then the floor holds: no stream of fetches.
    assert world.fetches.count(CLIENT_ID) == 2


@pytest.mark.parametrize("overrides", [
    pytest.param({"aud": "https://someone-else.example"}, id="wrong_aud"),
    pytest.param({"iss": "https://someone-else.example"}, id="wrong_iss"),
    pytest.param({"sub": "https://someone-else.example"}, id="wrong_sub"),
    pytest.param({"exp": int(time.time()) - 120, "iat": int(time.time()) - 150}, id="expired"),
    pytest.param({"exp": int(time.time()) + 600}, id="lives_too_long"),
])
def test_a_bad_client_assertion_is_refused(world, overrides):
    r = refused(world, client_assertion=world.client_assertion(**overrides))
    assert (r.code, r.status) == ("invalid_client", 401)


def test_a_client_assertion_addressed_to_the_token_endpoint_is_accepted(world):
    assert world.redeem(client_assertion=world.client_assertion(aud=TOKEN_ENDPOINT))


def test_an_hmac_client_assertion_is_refused(world):
    hmac_assertion = jwt.encode({"iss": CLIENT_ID, "sub": CLIENT_ID, "aud": ISSUER, "iat": int(time.time()),
                                 "exp": int(time.time()) + 60, "jti": "h1"},
                                "a-shared-secret-of-sufficient-length-for-hs256", algorithm="HS256")
    r = refused(world, client_assertion=hmac_assertion)
    assert r.status == 401 and r.reason == "bad_alg"


def test_unreachable_client_metadata_fails_closed(world):
    world.documents[CLIENT_ID] = MetadataError("timed out")
    r = refused(world)
    assert (r.code, r.status, r.reason) == ("invalid_client", 401, "client_metadata")


def test_metadata_naming_a_different_client_is_refused(world):
    world.documents[CLIENT_ID] = {**world.documents[CLIENT_ID], "client_id": "https://other.example/c.json"}
    assert refused(world).status == 401


def test_metadata_not_using_private_key_jwt_is_refused(world):
    world.documents[CLIENT_ID] = {**world.documents[CLIENT_ID], "token_endpoint_auth_method": "none"}
    assert refused(world).reason == "client_metadata"


# --- the ID-JAG ----------------------------------------------------------------------


@pytest.mark.parametrize("overrides", [
    pytest.param({"aud": "https://canopy.example"}, id="wrong_aud"),
    pytest.param({"iss": "https://canopy.example"}, id="wrong_iss"),
    pytest.param({"client_id": "https://other-client.example/c.json"}, id="other_client"),
    pytest.param({"resource": "https://other.example/mcp/"}, id="other_resource"),
    pytest.param({"exp": int(time.time()) - 120, "iat": int(time.time()) - 200}, id="expired"),
    pytest.param({"exp": int(time.time()) + 3600}, id="lives_too_long"),
    pytest.param({"scope": "admin:everything"}, id="unknown_scope"),
])
def test_a_bad_id_jag_is_refused(world, overrides):
    r = refused(world, assertion=world.id_jag(**overrides))
    assert r.status == 400 and r.code in ("invalid_grant", "invalid_scope")
    assert len(world.token_store) == 0


def test_an_id_jag_without_a_scope_is_refused(world):
    import uuid

    now = int(time.time())
    claims = {"iss": ISSUER, "aud": ISSUER, "sub": "42", "client_id": CLIENT_ID, "resource": RESOURCE,
              "iat": now, "exp": now + 60, "jti": str(uuid.uuid4())}
    token = jwt.encode(claims, world.host_key, algorithm="EdDSA",
                       headers={"kid": world.config.kid, "typ": contract.ID_JAG_TYP})
    assert refused(world, assertion=token).code == "invalid_grant"


def test_an_id_jag_not_signed_by_the_host_is_refused(world):
    """canopy holds no key the host trusts to name a user: a grant it signed
    itself, with its own client key, is worth nothing here."""
    r = refused(world, assertion=world.id_jag(key=world.client.client_key))
    assert r.code == "invalid_grant"


def test_an_id_jag_naming_an_unknown_kid_is_refused(world):
    r = refused(world, assertion=world.id_jag(headers={"kid": "someone-else", "typ": contract.ID_JAG_TYP}))
    assert r.reason == "idjag_unknown_key"


def test_an_id_jag_signed_with_the_wrong_algorithm_for_the_key_is_refused(world):
    other = ec.generate_private_key(ec.SECP256R1())
    r = refused(world, assertion=world.id_jag(key=other, alg="ES256"))
    assert r.code == "invalid_grant"


def test_an_id_jag_must_say_it_is_one(world):
    """A visitor assertion is signed by the same key; it must not double as a grant."""
    assertion = sign_visitor_assertion(world.config, "42")
    r = refused(world, assertion=assertion)
    assert (r.code, r.reason) == ("invalid_grant", "idjag_bad_typ")


def test_an_id_jag_for_an_inactive_subject_is_refused(world):
    token = world.id_jag()
    world.active.discard("42")
    r = refused(world, assertion=token)
    assert (r.code, r.reason) == ("invalid_grant", "idjag_sub")


def test_a_retired_host_key_still_verifies_during_its_window(world):
    old = world.host_key
    new = ed25519.Ed25519PrivateKey.generate()
    rotated = HostConfig(signing_key=private_pem(new), issuer=ISSUER, resource=RESOURCE,
                         token_endpoint=TOKEN_ENDPOINT, canopy_client_id=CLIENT_ID,
                         scope_tools={"marketplace:read": ["marketplace_orgs_get"]},
                         retired_keys=[private_pem(old)])
    world.handler.config = rotated
    assert world.redeem()  # still signed by the old key, named by its kid
    assert len(rotated.jwks()["keys"]) == 2


def test_an_id_jag_works_once(world):
    token = world.id_jag()
    world.redeem(assertion=token)
    r = refused(world, assertion=token)
    assert (r.code, r.reason) == ("invalid_grant", "replayed")
    assert len(world.token_store) == 1


def test_a_client_assertion_works_once(world):
    ca = world.client_assertion()
    world.redeem(client_assertion=ca)
    assert refused(world, client_assertion=ca).code == "invalid_grant"


def test_a_failed_redemption_does_not_burn_the_grant(world):
    """jtis are consumed only once every check passed."""
    token = world.id_jag()
    assert refused(world, assertion=token, proof=None).code == "invalid_dpop_proof"
    assert world.redeem(assertion=token)


def test_the_resource_must_be_this_mcp(world):
    assert refused(world, extra={"resource": "https://other.example/mcp/"}).code == "invalid_target"


def test_the_assertion_parameter_is_required(world):
    assert refused(world, drop=("assertion",)).code == "invalid_request"


def test_a_jti_store_that_cannot_answer_fails_closed(world):
    class Broken(MemoryJtiStore):
        def consume(self, entries):
            raise RuntimeError("db down")

    world.handler.jti_store = Broken()
    r = refused(world)
    assert (r.code, r.reason) == ("invalid_grant", "jti_store_error")
    assert len(world.token_store) == 0


# --- DPoP at the token endpoint -------------------------------------------------------------


def test_a_grant_without_a_dpop_proof_is_refused(world):
    r = refused(world, proof=None)
    assert r.code == "invalid_dpop_proof"
    assert "DPoP" in r.headers()["WWW-Authenticate"]


def test_two_dpop_proofs_are_refused(world):
    assert refused(world, proof=world.proof() + "," + world.proof()).code == "invalid_dpop_proof"


@pytest.mark.parametrize("make", [
    pytest.param(lambda w: w.proof(htu="https://labs.example.org/o/other/"), id="wrong_htu"),
    pytest.param(lambda w: w.proof(htm="GET"), id="wrong_htm"),
    pytest.param(lambda w: w.proof(iat=int(time.time()) - 600), id="stale"),
    pytest.param(lambda w: w.proof(iat=int(time.time()) + 600), id="from_the_future"),
    pytest.param(lambda w: w.proof(jwk=ec_jwk(ec.generate_private_key(ec.SECP256R1()))),
                 id="signed_by_another_key"),
    pytest.param(lambda w: w.proof(jwk={**ec_jwk(w.client.dpop_key), "d": "c2VjcmV0"}),
                 id="private_key_in_header"),
    pytest.param(lambda w: w.proof(access_token="some-token"), id="ath_at_token_endpoint"),
    pytest.param(lambda w: w.proof(headers={"typ": "JWT", "jwk": ec_jwk(w.client.dpop_key)}), id="wrong_typ"),
    pytest.param(lambda w: jwt.encode({"htm": "POST", "htu": TOKEN_ENDPOINT, "iat": int(time.time()), "jti": "j"},
                                      "secret-secret-secret-secret-secret!", algorithm="HS256",
                                      headers={"typ": "dpop+jwt", "jwk": ec_jwk(w.client.dpop_key)}), id="hmac"),
    pytest.param(lambda w: "not-a-jwt", id="garbage"),
])
def test_a_bad_dpop_proof_is_refused(world, make):
    r = refused(world, proof=make(world))
    assert (r.code, r.status) == ("invalid_dpop_proof", 400)
    assert len(world.token_store) == 0


def test_the_dpop_key_must_not_be_the_client_key(world):
    jwk = {k: v for k, v in world.client_jwk.items() if k in ("kty", "crv", "x")}
    proof = world.proof(key=world.client.client_key, jwk=jwk, alg="EdDSA")
    r = refused(world, proof=proof)
    assert (r.code, r.reason) == ("invalid_dpop_proof", "dpop_is_client_key")


def test_a_dpop_proof_works_once(world):
    proof = world.proof()
    world.redeem(proof=proof)
    assert refused(world, proof=proof).code == "invalid_grant"


def test_no_refusal_carries_a_credential(world):
    token = world.id_jag(aud="https://canopy.example")
    r = refused(world, assertion=token)
    assert token not in r.description and token not in str(r.body())
