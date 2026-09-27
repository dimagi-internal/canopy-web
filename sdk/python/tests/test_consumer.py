"""canopy's side: verifying a host's statements, and redeeming — against the
host half, in process."""
from __future__ import annotations

import time
import uuid

import jwt
import pytest

from canopy_sdk import consumer, contract
from canopy_sdk.host import authorization_server_metadata, issue_id_jag, sign_visitor_assertion
from canopy_sdk.keys import generate_private_key, public_pem

from .helpers import CANOPY, CLIENT_ID, ISSUER, RESOURCE, TOKEN_ENDPOINT, World


@pytest.fixture
def world():
    return World()


def _pub(world):
    return public_pem(world.host_key)


# --- the visitor assertion ----------------------------------------------------------


def test_an_sdk_signed_assertion_verifies(world):
    spent = []
    claims = consumer.verify_visitor_assertion(sign_visitor_assertion(world.config, "42"), [_pub(world)],
                                               audience=CANOPY, spend_jti=spent.append)
    assert claims["sub"] == "42" and spent == [claims]


def _assertion(key, **over):
    now = int(time.time())
    claims = {"iss": "connect-labs", "sub": "42", "aud": CANOPY, "iat": now, "exp": now + 60,
              "jti": str(uuid.uuid4())}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="EdDSA")


@pytest.mark.parametrize("over,code", [
    ({"aud": "https://elsewhere"}, "wrong_audience"),
    ({"exp": int(time.time()) - 100, "iat": int(time.time()) - 160}, "expired"),
    ({"jti": None}, "incomplete"),
    ({"exp": int(time.time()) + 3600}, "too_long"),
    ({"sub": " "}, "no_subject"),
])
def test_a_bad_assertion_is_refused_with_a_stable_code(world, over, code):
    with pytest.raises(consumer.AssertionRefused) as info:
        consumer.verify_visitor_assertion(_assertion(world.host_key, **over), [_pub(world)], audience=CANOPY)
    assert info.value.code == code


def test_an_assertion_by_an_unregistered_key_is_refused(world):
    with pytest.raises(consumer.AssertionRefused) as info:
        consumer.verify_visitor_assertion(_assertion(generate_private_key()), [_pub(world)], audience=CANOPY,
                                          label="connect-labs")
    assert info.value.code == "bad_signature" and "'connect-labs'" in info.value.message


def test_an_hmac_assertion_is_refused_even_with_the_public_key_as_secret(world):
    token = jwt.encode({"iss": "x", "sub": "1", "aud": CANOPY, "iat": int(time.time()),
                        "exp": int(time.time()) + 60, "jti": "j"}, "a" * 64, algorithm="HS256")
    with pytest.raises(consumer.AssertionRefused) as info:
        consumer.verify_visitor_assertion(token, ["a" * 64], audience=CANOPY)
    assert info.value.code == "bad_signature"


def test_the_issuer_is_read_without_trusting_it():
    assert consumer.unverified_issuer(jwt.encode({"iss": "site"}, "k" * 32, algorithm="HS256")) == "site"
    with pytest.raises(consumer.AssertionRefused):
        consumer.unverified_issuer(jwt.encode({"sub": "x"}, "k" * 32, algorithm="HS256"))
    with pytest.raises(consumer.AssertionRefused):
        consumer.unverified_issuer("garbage")


# --- the ID-JAG ------------------------------------------------------------------------


def _check(world, token, **kw):
    args = dict(issuer=ISSUER, client_id=CLIENT_ID, resource=RESOURCE, subject="42", label="connect-labs")
    args.update(kw)
    return consumer.check_id_jag(token, [_pub(world)], **args)


def test_an_sdk_issued_id_jag_passes_canopys_check(world):
    claims = _check(world, issue_id_jag(world.config, "42", ["marketplace:read"]))
    assert claims["scope"] == "marketplace:read"


@pytest.mark.parametrize("over,code", [
    ({"sub": "someone-else"}, "wrong_subject"),
    ({"client_id": "https://evil.test/oauth/client.json"}, "wrong_client"),
    ({"resource": "https://labs.example.org/other-mcp/"}, "wrong_resource"),
    ({"aud": "https://elsewhere.test"}, "wrong_audience"),
    ({"iss": "https://elsewhere.test"}, "wrong_issuer"),
    ({"exp": int(time.time()) + 3600}, "too_long"),
    ({"exp": int(time.time()) - 100, "iat": int(time.time()) - 220}, "expired"),
])
def test_a_bad_id_jag_is_refused_before_canopy_spends_it(world, over, code):
    with pytest.raises(consumer.RedemptionRefused) as info:
        _check(world, world.id_jag(**over))
    assert info.value.code == code


def test_the_id_jag_typ_is_checked_before_keys_are_resolved(world):
    resolved = []
    token = world.id_jag(headers={"kid": world.config.kid, "typ": "JWT"})
    with pytest.raises(consumer.RedemptionRefused) as info:
        consumer.check_id_jag(token, lambda: resolved.append(1) or [], issuer=ISSUER, client_id=CLIENT_ID,
                              resource=RESOURCE, subject="42")
    assert info.value.code == "wrong_type" and not resolved


def test_an_id_jag_by_another_key_is_bad_signature(world):
    with pytest.raises(consumer.RedemptionRefused) as info:
        _check(world, world.id_jag(key=generate_private_key()))
    assert info.value.code == "bad_signature"


def test_an_unreadable_id_jag_is_malformed(world):
    with pytest.raises(consumer.RedemptionRefused) as info:
        _check(world, "not-a-jwt")
    assert info.value.code == "malformed"


# --- what canopy signs ---------------------------------------------------------------------


def test_the_client_assertion_is_private_key_jwt(world):
    token = world.client.client_assertion(ISSUER)
    [jwk] = world.client.jwks()["keys"]
    claims = jwt.decode(token, jwt.PyJWK.from_dict(jwk).key, algorithms=["EdDSA"], audience=ISSUER)
    assert claims["iss"] == claims["sub"] == CLIENT_ID
    assert claims["exp"] - claims["iat"] <= 60 and claims["jti"]
    assert jwt.get_unverified_header(token)["kid"] == jwk["kid"]


def test_the_dpop_key_is_not_the_published_client_key(world):
    assert world.client.dpop_jkt not in {k["kid"] for k in world.client.jwks()["keys"]}


# --- redemption ---------------------------------------------------------------------------


def test_metadata_naming_another_issuer_is_refused():
    with pytest.raises(consumer.RedemptionRefused) as info:
        consumer.validate_authorization_server_metadata({"issuer": "https://evil"}, ISSUER)
    assert info.value.code == "issuer_mismatch"


@pytest.mark.parametrize("status,doc,code", [
    (400, {"error": "invalid_grant", "error_description": "leaky SECRET"}, "redeem_refused"),
    (200, {"token_type": "DPoP", "expires_in": 60}, "bad_response"),
    (200, {"access_token": "x", "token_type": "Bearer", "expires_in": 60}, "not_dpop"),
    (200, {"access_token": "x", "token_type": "DPoP"}, "bad_response"),
    (200, {"access_token": "x", "token_type": "DPoP", "expires_in": "soon"}, "bad_response"),
])
def test_a_bad_token_response_is_refused(status, doc, code):
    with pytest.raises(consumer.RedemptionRefused) as info:
        consumer.parse_token_response(status, doc)
    assert info.value.code == code and "SECRET" not in info.value.message


def test_a_long_lived_token_is_clamped_and_never_printed():
    token = consumer.parse_token_response(200, {"access_token": "SECRET", "token_type": "dpop",
                                                "expires_in": 86400})
    assert token.expires_in == 900 and "SECRET" not in repr(token)


def test_a_nonce_challenge_is_answered_once_with_fresh_statements():
    posts = []

    def post(url, data, headers, what=""):
        posts.append((dict(data), dict(headers)))
        if len(posts) == 1:
            return 400, {"error": "use_dpop_nonce"}, {"DPoP-Nonce": "n-1"}
        return 200, {"ok": 1}, {}

    counter = iter(range(10))
    status, doc = consumer.request_token(
        post, TOKEN_ENDPOINT, {"grant_type": "g"}, audience=ISSUER,
        client_assertion=lambda aud: f"ca-{next(counter)}",
        dpop_proof=lambda htm, htu, nonce=None: f"proof-{nonce}")
    assert status == 200 and len(posts) == 2
    assert posts[0][0]["client_assertion"] != posts[1][0]["client_assertion"]
    assert posts[1][1]["DPoP"] == "proof-n-1"


def test_canopy_redeems_an_sdk_host_grant_in_process(world):
    """The two halves, one call each: consumer.redeem_id_jag ↔ GrantHandler."""
    meta = authorization_server_metadata(world.config)

    def fetch_json(url):
        assert url == contract.metadata_url(ISSUER)
        return meta

    def post_form(url, data, headers, what=""):
        from canopy_sdk.host import GrantRefused

        assert url == TOKEN_ENDPOINT
        try:
            result = world.handler.handle(data, headers.get("DPoP"))
        except GrantRefused as refused:
            return refused.status, refused.body(), {}
        return 200, result.body(), {}

    id_jag = issue_id_jag(world.config, "42", ["marketplace:read"])
    token = consumer.redeem_id_jag(id_jag, issuer=ISSUER, resource=RESOURCE, credentials=world.client,
                                   fetch_json=fetch_json, post_form=post_form)
    assert token.scope == "marketplace:read" and token.expires_in <= 900

    principal = world.verifier.authenticate(
        consumer.dpop_authorization(token.access_token),
        [world.client.dpop_proof("POST", RESOURCE, access_token=token.access_token)], "POST")
    assert principal.subject == "42" and principal.client_id == CLIENT_ID

    with pytest.raises(consumer.RedemptionRefused) as info:
        consumer.redeem_id_jag(id_jag, issuer=ISSUER, resource=RESOURCE, credentials=world.client,
                               fetch_json=fetch_json, post_form=post_form)
    assert info.value.code == "redeem_refused" and "invalid_grant" in info.value.message
