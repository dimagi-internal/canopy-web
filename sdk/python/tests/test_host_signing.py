"""What a host signs, and the page registry that decides a grant's scopes.

Ported from connect-labs ``connect_labs/labs/tests/test_canopy_grant.py``. The
rule these pin: the SERVER decides whether canopy may act as the visitor, and
with what — nothing the browser says can add a scope, pick another user, or
conjure a grant on an unregistered page.
"""
from __future__ import annotations

import io
import json
import urllib.error

import jwt
import pytest
from jwt import PyJWK

from canopy_sdk import contract
from canopy_sdk.host import (
    HostConfig, HostNotConfigured, MintFailed, PageTokens, arrival_payload, issue_id_jag, mint_contact_token,
    sign_visitor_assertion,
)
from canopy_sdk.keys import generate_private_key, private_pem

from .helpers import CANOPY, CLIENT_ID, ISSUER, RESOURCE, TOKEN_ENDPOINT, World


@pytest.fixture
def world():
    return World()


def _decode(world, token, audience=ISSUER):
    return jwt.decode(token, PyJWK.from_dict(world.config.public_jwk).key, algorithms=["EdDSA"], audience=audience)


class TestTheIdJag:
    def test_it_matches_the_contract(self, world):
        token = issue_id_jag(world.config, "42", ["marketplace:read"])
        header = jwt.get_unverified_header(token)
        claims = _decode(world, token)
        assert header["typ"] == "oauth-id-jag+jwt" and header["alg"] == "EdDSA"
        assert header["kid"] == world.config.kid, "the key canopy already verifies assertions with"
        assert claims["iss"] == claims["aud"] == ISSUER, "the host grants for its own authorization server"
        assert claims["client_id"] == CLIENT_ID and claims["resource"] == RESOURCE
        assert claims["scope"] == "marketplace:read"
        assert claims["exp"] - claims["iat"] <= contract.ID_JAG_MAX_LIFETIME and claims["jti"]

    def test_its_subject_is_the_assertions_subject(self, world):
        grant = _decode(world, issue_id_jag(world.config, "42", ["marketplace:read"]))
        assertion = jwt.decode(sign_visitor_assertion(world.config, "42"), options={"verify_signature": False})
        assert grant["sub"] == assertion["sub"] == "42"

    def test_unknown_scopes_are_dropped_and_none_left_is_refused(self, world):
        with pytest.raises(ValueError):
            issue_id_jag(world.config, "42", ["admin:everything"])

    def test_no_canopy_client_means_no_id_jag(self, world):
        off = HostConfig(signing_key=world.host_key, issuer=ISSUER, resource=RESOURCE,
                         token_endpoint=TOKEN_ENDPOINT, scope_tools={"marketplace:read": ["x"]})
        with pytest.raises(HostNotConfigured):
            issue_id_jag(off, "42", ["marketplace:read"])


class TestTheAssertion:
    def test_it_matches_what_canopy_verifies(self, world):
        token = sign_visitor_assertion(world.config, "42", name="Gillian", email="g@example.org",
                                       email_verified=True)
        claims = _decode(world, token, audience=CANOPY)
        assert claims["iss"] == "connect-labs" and claims["sub"] == "42"
        assert claims["exp"] - claims["iat"] <= contract.ASSERTION_MAX_LIFETIME
        assert claims["email_verified"] is True and claims["name"] == "Gillian"
        assert jwt.get_unverified_header(token)["kid"] == world.config.kid

    def test_email_verified_is_never_claimed_for_an_empty_address(self, world):
        claims = jwt.decode(sign_visitor_assertion(world.config, "42", email_verified=True),
                            options={"verify_signature": False})
        assert claims["email_verified"] is False

    def test_extra_claims_cannot_override_the_contract(self, world):
        claims = jwt.decode(sign_visitor_assertion(world.config, "42", extra={"sub": "someone-else", "x": 1}),
                            options={"verify_signature": False})
        assert claims["sub"] == "42" and claims["x"] == 1

    def test_an_rsa_or_symmetric_signing_key_is_refused(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        with pytest.raises(contract.ContractError):
            HostConfig(signing_key=private_pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)))
        with pytest.raises(contract.ContractError):
            HostConfig(signing_key="not a pem")

    def test_lifetimes_above_the_contract_are_refused(self):
        key = generate_private_key()
        with pytest.raises(ValueError):
            HostConfig(signing_key=key, assertion_ttl=121)
        with pytest.raises(ValueError):
            HostConfig(signing_key=key, id_jag_ttl=301)
        with pytest.raises(ValueError):
            HostConfig(signing_key=key, access_token_ttl=901)


class TestTheArrival:
    def test_a_registered_page_sends_an_id_jag(self, world):
        payload = arrival_payload(world.config, "42", scopes=("marketplace:read",), agent_slug="ace")
        assert _decode(world, payload["id_jag"])["scope"] == "marketplace:read"
        assert payload["assertion"] and payload["agent_slug"] == "ace"

    def test_no_scopes_means_no_id_jag(self, world):
        assert "id_jag" not in arrival_payload(world.config, "42")

    def test_the_grant_switched_off_means_exactly_the_old_request(self, world):
        off = HostConfig(signing_key=world.host_key, canopy_base_url=CANOPY, app_name="connect-labs")
        payload = arrival_payload(off, "42", scopes=("marketplace:read",))
        assert set(payload) == {"assertion", "agent_slug"}

    def test_a_failed_id_jag_does_not_fail_the_arrival(self, world):
        payload = arrival_payload(world.config, "42", scopes=("admin:everything",))
        assert "id_jag" not in payload and payload["assertion"]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestTheMint:
    def test_it_posts_to_canopys_arrival_endpoint_and_returns_two_fields(self, world):
        sent = {}

        def opener(request, timeout):
            sent["url"], sent["body"] = request.full_url, json.loads(request.data)
            return _Resp(b'{"token": "t", "expires_at": "x", "contact_id": 9}')

        out = mint_contact_token(world.config, {"assertion": "a"}, opener=opener)
        assert out == {"token": "t", "expires_at": "x"}, "canopy's response is not passed through"
        assert sent["url"] == CANOPY + contract.ARRIVAL_PATH

    def test_canopys_reason_is_carried_on_a_refusal(self, world):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "no", {}, io.BytesIO(b"replayed: used"))

        with pytest.raises(MintFailed, match="replayed"):
            mint_contact_token(world.config, {}, opener=opener)

    def test_no_token_is_a_failure(self, world):
        with pytest.raises(MintFailed):
            mint_contact_token(world.config, {}, opener=lambda r, timeout: _Resp(b"{}"))


# --- the page token -------------------------------------------------------------------


PAGES = {"marketplace:network": ("marketplace:read",)}


@pytest.fixture
def pages():
    return PageTokens("server-secret", PAGES, scope_tools={"marketplace:read": ["x"]})


class TestThePageToken:
    def test_a_registered_page_yields_its_scopes(self, pages):
        assert pages.scopes(pages.issue("marketplace:network", 7), 7) == ("marketplace:read",)

    def test_an_unregistered_page_gets_no_token(self, pages):
        assert pages.issue("labs:somewhere_else", 7) == ""
        assert pages.issue("marketplace:network", None) == ""

    def test_a_forged_page_token_yields_nothing(self, pages):
        other = PageTokens("someone-elses-secret", PAGES)
        assert pages.scopes(other.issue("marketplace:network", 7), 7) == ()
        assert pages.scopes("garbage", 7) == ()
        assert pages.scopes("", 7) == ()
        assert pages.scopes("x" * 2000, 7) == ()

    def test_a_tampered_payload_yields_nothing(self, pages):
        token = pages.issue("marketplace:network", 7)
        payload, _, sig = token.partition(".")
        assert pages.scopes(payload[:-2] + "AA." + sig, 7) == ()

    def test_another_users_page_token_yields_nothing(self, pages):
        assert pages.scopes(pages.issue("marketplace:network", 7), 8) == ()

    def test_an_expired_page_token_yields_nothing(self, pages):
        token = pages.issue("marketplace:network", 7, now=1_000_000)
        assert pages.scopes(token, 7, now=1_000_000 + pages.max_age + 1) == ()

    def test_a_page_dropped_from_the_registry_stops_granting_at_once(self, pages):
        token = pages.issue("marketplace:network", 7)
        pages.pages.clear()
        assert pages.scopes(token, 7) == ()

    def test_every_page_scope_must_be_one_the_server_offers(self):
        with pytest.raises(ValueError):
            PageTokens("s", {"p": ("admin:everything",)}, scope_tools={"marketplace:read": ["x"]})
        with pytest.raises(ValueError):
            PageTokens("s", {"p": ()}, scope_tools={"marketplace:read": ["x"]})
        with pytest.raises(ValueError):
            PageTokens("", PAGES)
