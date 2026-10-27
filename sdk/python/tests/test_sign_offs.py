"""Site sign-offs (``canopy_sdk.sign_offs``): the request and the approval round-trip,
and every check fails closed."""
from __future__ import annotations

import json
import time

import jwt
import pytest
from canopy_sdk import sign_offs as wire
from canopy_sdk.fetch import FetchError
from canopy_sdk.keys import generate_private_key, public_pem

from .helpers import CANOPY, CLIENT_ID, World

SITE = "connect-labs"
ASK = dict(subject="Someone@Example.org", action_key="labs.mcp_token.full",
           title="Create a full-access labs MCP token", site_ref="pending-1",
           return_url="https://labs.example.org/labs/mcp/tokens/approved/")
SIGN_OFF = {"id": "so_1", "action_key": "labs.mcp_token.full", "site_ref": "pending-1",
            "approver": {"email": "someone@example.org", "name": "Some One"},
            "approved_at": "2026-10-27T12:00:00Z", "step_up": "passkey"}


@pytest.fixture
def world():
    return World()


def _approval(world, **overrides):
    body = {**SIGN_OFF, **overrides.pop("sign_off", {})}
    return wire.sign_approval(world.client.client_key, CLIENT_ID, SITE,
                              subject=overrides.pop("subject", "someone@example.org"), sign_off=body, **overrides)


# --- the request -----------------------------------------------------------------


def test_ask_round_trips_normalized_and_spends_its_jti(world):
    url = wire.create_url(CANOPY)
    token = wire.request_assertion(SITE, world.host_key, url, ask=wire.ask_payload(**ASK), workspace="connect")
    spent = []
    claims = wire.verify_request(token, [public_pem(world.host_key)], audiences=[url], asking=True,
                                 spend_jti=spent.append)
    assert claims["iss"] == claims["sub"] == SITE
    assert claims["canopy_workspace"] == "connect"
    assert claims["sign_off_request"]["subject"] == "someone@example.org"
    assert claims["sign_off_request"]["action"]["key"] == "labs.mcp_token.full"
    assert spent == [claims]


def test_a_status_read_cannot_be_used_to_ask(world):
    url = wire.create_url(CANOPY)
    token = wire.request_assertion(SITE, world.host_key, url)
    with pytest.raises(wire.SignOffError) as exc:
        wire.verify_request(token, [public_pem(world.host_key)], audiences=[url], asking=True)
    assert exc.value.code == "malformed"


def test_a_status_read_is_bound_to_its_id(world):
    token = wire.request_assertion(SITE, world.host_key, wire.status_url(CANOPY, "so_1"))
    with pytest.raises(wire.SignOffError) as exc:
        wire.verify_request(token, [public_pem(world.host_key)],
                            audiences=[wire.status_url(CANOPY, "so_2")], asking=False)
    assert exc.value.code == "wrong_audience"


@pytest.mark.parametrize("change,code", [
    ({"return_url": "http://labs.example.org/back"}, "bad_return_url"),
    ({"subject": "not-an-email"}, "malformed"),
    ({"title": "x" * 201}, "malformed"),
    ({"site_ref": ""}, "malformed"),
])
def test_ask_payload_refusals(change, code):
    with pytest.raises(wire.SignOffError) as exc:
        wire.ask_payload(**{**ASK, **change})
    assert exc.value.code == code


@pytest.mark.parametrize("mutate,code", [
    (lambda c: c.update(exp=c["iat"] + 600), "too_long"),
    (lambda c: c.update(sub="someone-else"), "bad_subject"),
    (lambda c: c.pop("jti"), "incomplete"),
])
def test_request_refusals(world, mutate, code):
    url = wire.create_url(CANOPY)
    now = int(time.time())
    claims = {"iss": SITE, "sub": SITE, "aud": url, "iat": now, "exp": now + 60, "jti": "j",
              "sign_off_request": wire.ask_payload(**ASK)}
    mutate(claims)
    token = jwt.encode(claims, world.host_key, algorithm="EdDSA", headers={"typ": wire.REQUEST_TYP})
    with pytest.raises(wire.SignOffError) as exc:
        wire.verify_request(token, [public_pem(world.host_key)], audiences=[url], asking=True)
    assert exc.value.code == code


def test_another_sites_key_does_not_verify(world):
    url = wire.create_url(CANOPY)
    token = wire.request_assertion(SITE, generate_private_key("EdDSA"), url, ask=wire.ask_payload(**ASK))
    with pytest.raises(wire.SignOffError) as exc:
        wire.verify_request(token, [public_pem(world.host_key)], audiences=[url], asking=True)
    assert exc.value.code == "bad_signature"


def test_request_sign_off_posts_signed_and_reads_the_answer(world):
    seen = {}

    def post(url, payload, *, headers):
        bearer = headers["Authorization"].removeprefix("Bearer ")
        seen["claims"] = wire.verify_request(bearer, [public_pem(world.host_key)], audiences=[url], asking=True)
        return 201, json.dumps({"id": "so_1", "approve_url": "https://canopy/w/connect/sign-offs/so_1",
                                "expires_at": "2026-10-27T12:15:00Z", "approvers": "subject"}).encode(), {}

    asked = wire.request_sign_off(CANOPY, SITE, world.host_key, workspace="connect", post=post, **ASK)
    assert asked == wire.SignOffRequest("so_1", "https://canopy/w/connect/sign-offs/so_1",
                                        "2026-10-27T12:15:00Z", "subject")
    assert seen["claims"]["sign_off_request"]["site_ref"] == "pending-1"


def test_no_approver_is_passed_through(world):
    def post(url, payload, *, headers):
        return 422, json.dumps({"detail": "no_approver: nobody in canopy may approve for this account"}).encode(), {}

    with pytest.raises(wire.SignOffError) as exc:
        wire.request_sign_off(CANOPY, SITE, world.host_key, post=post, **ASK)
    assert exc.value.code == "no_approver"
    assert exc.value.status == 422


def test_unreachable(world):
    def post(url, payload, *, headers):
        raise FetchError("connection refused")

    with pytest.raises(wire.SignOffError) as exc:
        wire.request_sign_off(CANOPY, SITE, world.host_key, post=post, **ASK)
    assert exc.value.code == "unreachable"


def test_fetch_status_reads_the_state_and_approval(world):
    approval = _approval(world)

    def get(url, *, headers, accept):
        bearer = headers["Authorization"].removeprefix("Bearer ")
        wire.verify_request(bearer, [public_pem(world.host_key)], audiences=[url], asking=False)
        assert url == wire.status_url(CANOPY, "so_1")
        return json.dumps({"id": "so_1", "state": "approved", "expires_at": "x", "approval": approval})

    got = wire.fetch_status(CANOPY, SITE, world.host_key, "so_1", get=get)
    assert got.state == "approved"
    assert got.approval == approval


# --- the approval ----------------------------------------------------------------


def test_approval_round_trips(world):
    got = wire.verify_approval(_approval(world), client_id=CLIENT_ID, site_name=SITE,
                               client_keys=world.resolver, expect_action="labs.mcp_token.full",
                               expect_site_ref="pending-1", expect_subject="SOMEONE@example.org")
    assert got.subject == "someone@example.org"
    assert got.approver_email == "someone@example.org"
    assert got.step_up == "passkey"
    assert got.jti


@pytest.mark.parametrize("kwargs,code", [
    ({"expect_action": "labs.other"}, "wrong_action"),
    ({"expect_site_ref": "pending-2"}, "wrong_site_ref"),
    ({"expect_subject": "else@example.org"}, "wrong_subject"),
])
def test_approval_must_match_what_the_site_is_doing(world, kwargs, code):
    args = {"expect_action": "labs.mcp_token.full", "expect_site_ref": "pending-1", **kwargs}
    with pytest.raises(wire.SignOffError) as exc:
        wire.verify_approval(_approval(world), client_id=CLIENT_ID, site_name=SITE,
                             client_keys=world.resolver, **args)
    assert exc.value.code == code


def test_approval_for_another_site_is_refused(world):
    token = wire.sign_approval(world.client.client_key, CLIENT_ID, "another-site",
                               subject="someone@example.org", sign_off=SIGN_OFF)
    with pytest.raises(wire.SignOffError):
        wire.verify_approval(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver,
                             expect_action="labs.mcp_token.full", expect_site_ref="pending-1")


def test_approval_without_a_passkey_step_up_is_refused(world):
    with pytest.raises(wire.SignOffError) as exc:
        wire.verify_approval(_approval(world, sign_off={"step_up": "password"}), client_id=CLIENT_ID,
                             site_name=SITE, client_keys=world.resolver,
                             expect_action="labs.mcp_token.full", expect_site_ref="pending-1")
    assert exc.value.code == "no_step_up"


def test_approval_signed_by_someone_else_is_refused(world):
    token = wire.sign_approval(generate_private_key("EdDSA"), CLIENT_ID, SITE,
                               subject="someone@example.org", sign_off=SIGN_OFF)
    with pytest.raises(wire.SignOffError):
        wire.verify_approval(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver,
                             expect_action="labs.mcp_token.full", expect_site_ref="pending-1")


def test_expired_approval_is_refused(world):
    token = _approval(world, now=time.time() - 3600)
    with pytest.raises(wire.SignOffError) as exc:
        wire.verify_approval(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver,
                             expect_action="labs.mcp_token.full", expect_site_ref="pending-1")
    assert exc.value.code == "expired"


def test_an_approval_cannot_outlive_ten_minutes(world):
    with pytest.raises(ValueError):
        _approval(world, lifetime=3600)
