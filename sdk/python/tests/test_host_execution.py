"""``verify_execution``: the host's half of ``Canopy-Execution``, against the
statement ``consumer.execution_statement`` signs (canopy's half). Mostly refusals:
each statement fails exactly one check."""
from __future__ import annotations

import time

import jwt
import pytest

from canopy_sdk import consumer, contract
from canopy_sdk.host import Execution, ExecutionRefused, verify_execution
from canopy_sdk.host.asgi import presented_dpop_jkt
from canopy_sdk.stores import MemoryJtiStore

from .helpers import CLIENT_ID, RESOURCE, World

TOKEN = "host-access-token"
FACTS = {
    "agent": "labsdata", "capability": "data",
    "declaration": {"data": "phi", "capability": "data",
                    "declared_by": "jj@dimagi.com", "declared_at": "2026-10-22T00:00:00+00:00"},
    "runner": {"id": "r-1", "name": "cloud-zdr-1",
               "flags": [{"flag": "zdr", "declared_by": "jj@dimagi.com", "declared_at": "x"}]},
    "runner_posture": {"data_mode": "zdr", "config_hash": "abc"},
    "requirements": ["zdr"], "turn": "t-1", "session": "s-1",
}


@pytest.fixture
def world():
    return World()


def _jkt(world):
    return consumer.dpop_jkt(world.client.dpop_key)


def _statement(world, facts=FACTS, *, audience=RESOURCE, token=TOKEN, key=None, **over) -> str:
    raw = consumer.execution_statement(key or world.client.client_key, CLIENT_ID, audience, facts,
                                       jkt=over.pop("jkt", _jkt(world)), access_token=token)
    if over:
        claims = {**jwt.decode(raw, options={"verify_signature": False}), **over}
        raw = jwt.encode(claims, world.client.client_key, algorithm="EdDSA",
                         headers={"kid": world.client_jwk["kid"], "typ": contract.EXECUTION_TYP})
    return raw


def _headers(statement, token=TOKEN):
    return {contract.EXECUTION_HEADER: statement, "Authorization": f"DPoP {token}"}


def _verify(world, headers, store=None, **kw):
    kw.setdefault("jkt", _jkt(world))
    return verify_execution(headers, config=world.config, client_keys=world.resolver,
                            replay_store=store or MemoryJtiStore(), **kw)


def _refused(world, headers, **kw) -> str:
    with pytest.raises(ExecutionRefused) as info:
        _verify(world, headers, **kw)
    return info.value.code


def test_a_statement_verifies_and_clears_phi(world):
    ex = _verify(world, _headers(_statement(world)))
    assert isinstance(ex, Execution)
    assert (ex.agent, ex.agent_sha, ex.turn, ex.session) == ("labsdata", "", "t-1", "s-1")
    assert ex.runner_flags == {"zdr"} and ex.requirements == ("zdr",)
    assert ex.phi_clearance(agents={"labsdata"}) is None


def test_the_presented_dpop_key_is_the_default_binding(world):
    token = presented_dpop_jkt.set(_jkt(world))
    try:
        ex = verify_execution(_headers(_statement(world)), config=world.config,
                              client_keys=world.resolver, replay_store=MemoryJtiStore())
    finally:
        presented_dpop_jkt.reset(token)
    assert ex.agent == "labsdata"


def test_an_object_with_headers_is_a_request(world):
    class Request:
        headers = _headers(_statement(world))

    assert _verify(world, Request()).agent == "labsdata"


def test_no_statement(world):
    assert _refused(world, {"Authorization": f"DPoP {TOKEN}"}) == "no_statement"


def test_a_statement_is_spent_once(world):
    store = MemoryJtiStore()
    headers = _headers(_statement(world))
    _verify(world, headers, store=store)
    assert _refused(world, headers, store=store) == "replayed"


def test_a_statement_for_another_resource(world):
    assert _refused(world, _headers(_statement(world, audience="https://other.test/mcp/"))) == "bad_audience"


def test_a_statement_signed_by_another_key(world):
    from canopy_sdk.keys import generate_private_key

    assert _refused(world, _headers(_statement(world, key=generate_private_key("EdDSA")))) in (
        "bad_signature", "unknown_key")


def test_a_statement_bound_to_another_dpop_key(world):
    assert _refused(world, _headers(_statement(world)), jkt="another-key") == "unbound"
    assert _refused(world, _headers(_statement(world)), jkt="") == "unbound"


def test_a_statement_for_another_access_token(world):
    assert _refused(world, _headers(_statement(world), token="a-different-token")) == "unbound"


def test_a_statement_living_too_long(world):
    now = int(time.time())
    assert _refused(world, _headers(_statement(world, iat=now, exp=now + 3600))) == "too_long"


def test_a_statement_from_another_issuer(world):
    assert _refused(world, _headers(_statement(world, iss="https://evil.test/oauth/client.json"))) == "bad_issuer"


def test_another_token_type_is_not_a_statement(world):
    raw = world.client_assertion(aud=RESOURCE)  # a real canopy JWT, the wrong typ
    assert _refused(world, _headers(raw)) == "bad_typ"


@pytest.mark.parametrize("change, code", [
    ({"agent": "ace"}, "agent_not_allowed"),
    ({"declaration": None}, "undeclared"),
    ({"declaration": {**FACTS["declaration"], "data": "pii"}}, "undeclared"),
    ({"runner": {**FACTS["runner"], "flags": []}}, "runner_flags"),
    ({"runner_posture": {"data_mode": ""}}, "runner_posture"),
    ({"requirements": []}, "requirements"),
])
def test_phi_clearance_needs_every_fact(world, change, code):
    ex = _verify(world, _headers(_statement(world, {**FACTS, **change})))
    assert ex.phi_clearance(agents={"labsdata"}) == code
