"""The host's MCP side: ``ResourceVerifier`` and the ASGI ``DPoPGate``.

Ported from the MCP half of connect-labs' ``test_delegation.py``: a bound token
needs a proof by its own key, is refused as a plain bearer, and every bad proof
is a 401 ``invalid_dpop_proof``.
"""
from __future__ import annotations

import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from canopy_sdk import contract
from canopy_sdk.contract import ContractError
from canopy_sdk.host import DPoPGate, DPoPRefused

from .helpers import BASE, RESOURCE, World, call_asgi, echo_app, ec_jwk


@pytest.fixture
def world():
    return World()


@pytest.fixture
def token(world):
    return world.redeem().access_token


def test_a_bound_token_needs_a_proof_by_its_own_key(world, token):
    jkt = contract.jwk_thumbprint(ec_jwk(world.client.dpop_key))
    assert world.verifier.resolve(token, None) is None, "a bound token is not a bearer token"
    assert world.verifier.resolve(token, "some-other-thumbprint") is None
    principal = world.verifier.resolve(token, jkt)
    assert principal.subject == "42"
    assert principal.scopes == ("marketplace:read",)
    assert principal.claims()["cnf"] == {"jkt": jkt}
    assert principal.claims()["act"] == {"sub": world.config.canopy_client_id}
    assert principal.allowed_tools == {"marketplace_orgs_get", "marketplace_rounds_list"}


def test_an_unknown_token_resolves_to_nothing(world):
    assert world.verifier.resolve("never-issued", "x") is None
    assert world.verifier.resolve("", "x") is None


def test_an_expired_delegated_token_is_refused(world, token):
    jkt = contract.jwk_thumbprint(ec_jwk(world.client.dpop_key))
    assert world.verifier.resolve(token, jkt, now=time.time() + 901) is None


def test_a_token_for_a_deactivated_subject_is_refused(world, token):
    jkt = contract.jwk_thumbprint(ec_jwk(world.client.dpop_key))
    world.active.discard("42")
    assert world.verifier.resolve(token, jkt) is None


def test_the_principal_filters_tools_to_its_scopes(world, token):
    principal = world.verifier.authenticate(f"DPoP {token}", [world.proof(htu=RESOURCE, access_token=token)],
                                            "POST")
    tools = [{"name": "marketplace_orgs_get"}, {"name": "list_templates"}, "marketplace_rounds_list"]
    assert principal.filter_tools(tools) == [{"name": "marketplace_orgs_get"}, "marketplace_rounds_list"]
    assert not principal.allows("list_templates")


def test_authenticate_leaves_non_dpop_requests_to_the_host(world, token):
    assert world.verifier.authenticate(f"Bearer {token}", [], "POST") is None
    assert world.verifier.authenticate(None, [], "POST") is None


def test_authenticate_refuses_an_unknown_token_with_a_good_proof(world):
    with pytest.raises(ContractError) as info:
        world.verifier.authenticate("DPoP nope", [world.proof(htu=RESOURCE, access_token="nope")], "POST")
    assert info.value.code == "invalid_token"


@pytest.mark.parametrize("make", [
    pytest.param(lambda w, t: w.proof(htu=RESOURCE, access_token="another-token"), id="wrong_ath"),
    pytest.param(lambda w, t: w.proof(htu=RESOURCE), id="no_ath"),
    pytest.param(lambda w, t: w.proof(htm="GET", htu=RESOURCE, access_token=t), id="wrong_htm"),
    pytest.param(lambda w, t: w.proof(htu=f"{BASE}/o/token/", access_token=t), id="wrong_htu"),
    pytest.param(lambda w, t: w.proof(htu=RESOURCE, access_token=t, iat=int(time.time()) - 120), id="stale"),
])
def test_a_bad_proof_is_refused_by_the_verifier(world, token, make):
    with pytest.raises(DPoPRefused):
        world.verifier.check_proof([make(world, token)], "POST", token)


def test_exactly_one_proof_is_required(world, token):
    proof = world.proof(htu=RESOURCE, access_token=token)
    with pytest.raises(DPoPRefused):
        world.verifier.check_proof([], "POST", token)
    with pytest.raises(DPoPRefused):
        world.verifier.check_proof([proof, proof], "POST", token)


def test_a_replay_store_that_cannot_answer_fails_closed(world, token):
    class Broken:
        def consume(self, entries):
            raise RuntimeError("down")

        def prune(self):
            pass

    world.verifier.replay_store = Broken()
    with pytest.raises(DPoPRefused) as info:
        world.verifier.check_proof([world.proof(htu=RESOURCE, access_token=token)], "POST", token)
    assert info.value.code == "replay_store_error"


# --- the ASGI gate ---------------------------------------------------------------------


def _post(app, headers):
    return call_asgi(app, "POST", "/mcp/", headers=headers,
                     body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode())


def test_the_gate_verifies_and_hands_the_app_a_bearer(world, token):
    seen = []
    status, _, _ = _post(DPoPGate(echo_app(seen), world.verifier),
                         {"Authorization": f"DPoP {token}", "DPoP": world.proof(htu=RESOURCE, access_token=token)})
    assert status == 200
    [request] = seen
    assert request["headers"][b"authorization"] == f"Bearer {token}".encode()
    assert b"dpop" not in request["headers"], "the proof is consumed here, not passed on"
    assert request["jkt"] == contract.jwk_thumbprint(ec_jwk(world.client.dpop_key))


def test_the_gate_passes_ordinary_bearers_through_untouched(world):
    seen = []
    status, _, _ = _post(DPoPGate(echo_app(seen), world.verifier), {"Authorization": "Bearer a-pat"})
    assert status == 200 and seen[0]["jkt"] is None
    assert seen[0]["headers"][b"authorization"] == b"Bearer a-pat"


def test_a_bound_token_sent_as_a_plain_bearer_is_refused_at_the_gate(world, token):
    # RFC 9449 §7.1. Left to the host's own verifier it only works on a host whose
    # verifier refuses an unknown bearer outright; canopy's live probe found one
    # that let it reach the tool layer (2026-09-28).
    seen = []
    status, headers, _ = _post(DPoPGate(echo_app(seen), world.verifier), {"Authorization": f"Bearer {token}"})
    assert status == 401 and seen == []
    assert "invalid_token" in dict(headers).get("www-authenticate", "")


def test_a_bound_token_with_no_proof_is_refused(world, token):
    seen = []
    status, headers, body = _post(DPoPGate(echo_app(seen), world.verifier), {"Authorization": f"DPoP {token}"})
    assert status == 401 and not seen
    assert json.loads(body)["error"] == "invalid_dpop_proof"
    assert 'DPoP error="invalid_dpop_proof"' in headers["www-authenticate"]


@pytest.mark.parametrize("make", [
    pytest.param(lambda w, t: w.proof(htu=RESOURCE, access_token="another-token"), id="wrong_ath"),
    pytest.param(lambda w, t: w.proof(htu=RESOURCE), id="no_ath"),
    pytest.param(lambda w, t: w.proof(htm="GET", htu=RESOURCE, access_token=t), id="wrong_htm"),
    pytest.param(lambda w, t: w.proof(htu=f"{BASE}/o/token/", access_token=t), id="wrong_htu"),
    pytest.param(lambda w, t: w.proof(htu=RESOURCE, access_token=t, iat=int(time.time()) - 120), id="stale"),
])
def test_a_bad_proof_at_the_mcp_is_refused(world, token, make):
    status, _, body = _post(DPoPGate(echo_app([]), world.verifier),
                            {"Authorization": f"DPoP {token}", "DPoP": make(world, token)})
    assert status == 401 and json.loads(body)["error"] == "invalid_dpop_proof"


def test_a_proof_by_a_key_the_token_is_not_bound_to_is_refused(world, token):
    other = ec.generate_private_key(ec.SECP256R1())
    proof = world.proof(htu=RESOURCE, access_token=token, key=other, jwk=ec_jwk(other))
    gate = DPoPGate(echo_app([]), world.verifier, require_principal=True)
    status, _, body = _post(gate, {"Authorization": f"DPoP {token}", "DPoP": proof})
    assert status == 401 and json.loads(body)["error"] == "invalid_token"


def test_a_replayed_proof_at_the_mcp_is_refused(world, token):
    gate = DPoPGate(echo_app([]), world.verifier)
    proof = world.proof(htu=RESOURCE, access_token=token)
    first = _post(gate, {"Authorization": f"DPoP {token}", "DPoP": proof})
    second = _post(gate, {"Authorization": f"DPoP {token}", "DPoP": proof})
    assert first[0] == 200
    assert second[0] == 401 and json.loads(second[2])["error"] == "invalid_dpop_proof"


def test_require_principal_publishes_who_the_request_acts_for(world, token):
    seen = []
    gate = DPoPGate(echo_app(seen), lambda: world.verifier, require_principal=True)
    status, _, _ = _post(gate, {"Authorization": f"DPoP {token}",
                                "DPoP": world.proof(htu=RESOURCE, access_token=token)})
    assert status == 200 and seen[0]["principal"].subject == "42"


def test_non_http_scopes_pass_through(world):
    import asyncio

    called = []

    async def app(scope, receive, send):
        called.append(scope["type"])

    asyncio.run(DPoPGate(app, world.verifier)({"type": "lifespan"}, None, None))
    assert called == ["lifespan"]
