"""``verify_subject``: the host's half of the subject statement (an agent's turn with ONE of
the host's own contacts, no grant), against what ``consumer.subject_statement`` signs.
Mostly refusals: each request fails exactly one check."""
from __future__ import annotations

import time

import jwt
import pytest

from canopy_sdk import consumer, contract
from canopy_sdk.host import Subject, SubjectRefused, verify_execution, verify_subject
from canopy_sdk.keys import generate_private_key
from canopy_sdk.stores import MemoryJtiStore

from .helpers import CLIENT_ID, RESOURCE, World

FACTS = {"sub": "personalid:ana", "site": "connect-messaging", "agent": "coach", "turn": "t-1",
         "session": "s-1", "capability": "messaging"}
RPC = ("tools/call", {"name": "worker_profile", "arguments": {}})


@pytest.fixture
def world():
    return World()


def _statement(world, facts=FACTS, *, audience=RESOURCE, key=None, rpc=None, **over) -> str:
    raw = consumer.subject_statement(key or world.client.client_key, CLIENT_ID, audience,
                                     jkt=over.pop("jkt", world.client.dpop_jkt),
                                     rpc=contract.rpc_digest(*(rpc or RPC)), **facts)
    if over:
        claims = {**jwt.decode(raw, options={"verify_signature": False}), **over}
        for k in [k for k, v in claims.items() if v is None]:
            del claims[k]
        raw = jwt.encode(claims, world.client.client_key, algorithm="EdDSA",
                         headers={"kid": world.client_jwk["kid"], "typ": over.get("_typ", contract.SUBJECT_TYP)})
    return raw


def _headers(world, statement, *, proof=None, htm="POST", htu=RESOURCE):
    return {"Authorization": consumer.subject_authorization(statement),
            contract.DPOP_HEADER: proof if proof is not None else world.client.dpop_proof(htm, htu)}


def _verify(world, headers, store=None, **kw):
    kw.setdefault("rpc", RPC)
    return verify_subject(headers, config=world.config, client_keys=world.resolver,
                          replay_store=store or MemoryJtiStore(), **kw)


def _refused(world, headers, **kw) -> str:
    with pytest.raises(SubjectRefused) as info:
        _verify(world, headers, **kw)
    return info.value.code


def test_a_statement_verifies_and_names_the_person(world):
    s = _verify(world, _headers(world, _statement(world)))
    assert isinstance(s, Subject)
    assert (s.site, s.sub, s.agent, s.turn, s.session, s.capability) == (
        "connect-messaging", "personalid:ana", "coach", "t-1", "s-1", "messaging")
    assert s.jkt == world.client.dpop_jkt and s.audit()["subject"] == "personalid:ana"
    assert jwt.get_unverified_header(_statement(world))["typ"] == "canopy-subject+jwt"


def test_its_execution_statement_verifies_with_the_subjects_key_and_says_sub(world):
    statement = consumer.execution_statement(world.client.client_key, CLIENT_ID, RESOURCE,
                                             {"agent": "coach", "runner": {}, "requirements": [], "turn": "t-1",
                                              "session": "s-1", "sub": "personalid:ana"},
                                             jkt=world.client.dpop_jkt)
    headers = {**_headers(world, _statement(world)), contract.EXECUTION_HEADER: statement}
    s = _verify(world, headers)
    ex = verify_execution(headers, config=world.config, client_keys=world.resolver,
                          replay_store=MemoryJtiStore(), jkt=s.jkt)
    assert ex.sub == s.sub == "personalid:ana"


def test_without_a_statement_or_with_a_bearer(world):
    assert _refused(world, {}) == "no_statement"
    assert _refused(world, {"Authorization": "Bearer x", "DPoP": world.client.dpop_proof("POST", RESOURCE)}) \
        == "no_statement"
    assert _refused(world, {"Authorization": "CanopySubject a,b"}) == "no_statement"


def test_a_statement_for_another_host_or_from_another_canopy(world):
    assert _refused(world, _headers(world, _statement(world, audience="https://other.example.org/mcp/"))) \
        == "bad_audience"
    stranger = generate_private_key("EdDSA")
    raw = consumer.subject_statement(stranger, CLIENT_ID, RESOURCE, jkt=world.client.dpop_jkt, **FACTS)
    assert _refused(world, _headers(world, raw)) in ("unknown_key", "bad_signature")
    assert _refused(world, _headers(world, _statement(world, iss="https://evil.example.org/oauth/client.json"))) \
        == "bad_issuer"


def test_another_statement_type_is_not_a_subject(world):
    execution = consumer.execution_statement(world.client.client_key, CLIENT_ID, RESOURCE,
                                             {"agent": "a", "runner": {}, "requirements": [], "turn": "t"},
                                             jkt=world.client.dpop_jkt)
    assert _refused(world, _headers(world, execution)) == "bad_typ"


def test_an_expired_or_long_lived_statement(world):
    old = int(time.time()) - 600
    assert _refused(world, _headers(world, _statement(world, iat=old, exp=old + 60))) == "expired"
    now = int(time.time())
    assert _refused(world, _headers(world, _statement(world, iat=now, exp=now + 3600))) in ("too_long", "lifetime")


def test_dpop_is_required_and_bound(world):
    statement = _statement(world)
    assert _refused(world, {"Authorization": consumer.subject_authorization(statement)}) == "no_proof"
    # A proof by another key than the one the statement is bound to.
    other = consumer.dpop_proof(generate_private_key("EdDSA"), "POST", RESOURCE)
    assert _refused(world, _headers(world, statement, proof=other)) == "unbound"
    # For another method or another URL.
    assert _refused(world, _headers(world, statement, htm="GET")) == "bad_proof"
    assert _refused(world, _headers(world, statement, htu="https://other.example.org/mcp/")) == "bad_proof"
    # A proof that rides an access token (ath) is not a subject request's.
    with_ath = world.client.dpop_proof("POST", RESOURCE, access_token="tok")
    assert _refused(world, _headers(world, statement, proof=with_ath)) == "bad_proof"
    # A statement bound to no key at all.
    assert _refused(world, _headers(world, _statement(world, cnf={}))) == "unbound"


def test_the_statement_rides_only_its_own_request(world):
    headers = _headers(world, _statement(world))
    assert _refused(world, headers, rpc=("tools/call", {"name": "worker_payments", "arguments": {}})) \
        == "wrong_request"


def test_a_replayed_statement_or_proof_is_refused(world):
    store = MemoryJtiStore()
    statement, proof = _statement(world), world.client.dpop_proof("POST", RESOURCE)
    _verify(world, _headers(world, statement, proof=proof), store=store)
    with pytest.raises(SubjectRefused) as info:
        _verify(world, _headers(world, statement, proof=world.client.dpop_proof("POST", RESOURCE)), store=store)
    assert info.value.code == "replayed"
    with pytest.raises(SubjectRefused) as info:
        _verify(world, _headers(world, _statement(world), proof=proof), store=store)
    assert info.value.code == "replayed"


def test_a_store_that_cannot_answer_fails_closed(world):
    class Broken:
        def consume(self, entries):
            raise RuntimeError("down")

        def prune(self):
            pass

    assert _refused(world, _headers(world, _statement(world)), store=Broken()) == "jti_store_error"


@pytest.mark.parametrize("missing", ["sub", "site", "agent", "turn", "session"])
def test_every_named_fact_is_required(world, missing):
    code = _refused(world, _headers(world, _statement(world, **{missing: None})))
    assert code in ("incomplete", "missing_claim")


def test_a_host_with_no_canopy_trusts_none(world):
    from canopy_sdk.host import HostConfig

    with pytest.raises(SubjectRefused) as info:
        verify_subject(_headers(world, _statement(world)), config=HostConfig(signing_key=world.host_key),
                       client_keys=world.resolver, replay_store=MemoryJtiStore())
    assert info.value.code == "not_configured"


def test_canopy_will_not_sign_one_without_a_person():
    with pytest.raises(ValueError):
        consumer.subject_statement(generate_private_key("EdDSA"), CLIENT_ID, RESOURCE, jkt="x",
                                   **{**FACTS, "sub": ""})


def test_a_host_checks_a_surfaces_statement_against_that_surfaces_resource(world):
    """One host config, two resources: a statement for a surface verifies only when the host
    names that surface's resource, and one for the host's own resource never passes there."""
    surface = "https://host.example.org/workers/mcp/"
    headers = _headers(world, _statement(world, audience=surface), htu=surface)
    assert _verify(world, headers, resource=surface).sub
    assert _refused(world, _headers(world, _statement(world, audience=surface), htu=surface)) == "bad_audience"
    assert _refused(world, _headers(world, _statement(world)), resource=surface) == "bad_audience"
