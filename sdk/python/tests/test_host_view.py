"""``verify_view``: the host's half of ``Canopy-View``, against the statement
``consumer.view_statement`` signs (canopy's half). Mostly refusals: each statement
fails exactly one check — and NO statement is a refusal, never "the person"."""
from __future__ import annotations

import time

import jwt
import pytest

from canopy_sdk import consumer, contract
from canopy_sdk.host import View, ViewRefused, same_resource, verify_view
from canopy_sdk.host.asgi import presented_dpop_jkt
from canopy_sdk.stores import MemoryJtiStore

from .helpers import CLIENT_ID, RESOURCE, World

TOKEN = "host-access-token"
URI = "labs-result://abc123"
PERSON = "42"


@pytest.fixture
def world():
    return World()


def _jkt(world):
    return consumer.dpop_jkt(world.client.dpop_key)


def _statement(world, *, audience=RESOURCE, token=TOKEN, key=None, subject=PERSON, uri=URI,
               session="s-1", rpc=None, **over) -> str:
    raw = consumer.view_statement(key or world.client.client_key, CLIENT_ID, audience,
                                  subject=subject, uri=uri, session=session,
                                  jkt=over.pop("jkt", _jkt(world)), access_token=token,
                                  viewer="user:7", agent="labsdata", rpc=rpc)
    if over:
        claims = {**jwt.decode(raw, options={"verify_signature": False}), **over}
        claims = {k: v for k, v in claims.items() if v is not None}
        raw = jwt.encode(claims, world.client.client_key, algorithm="EdDSA",
                         headers={"kid": world.client_jwk["kid"], "typ": contract.VIEW_TYP})
    return raw


def _headers(statement, token=TOKEN, **extra):
    return {contract.VIEW_HEADER: statement, "Authorization": f"DPoP {token}", **extra}


def _verify(world, headers, store=None, **kw):
    kw.setdefault("jkt", _jkt(world))
    kw.setdefault("uri", URI)
    kw.setdefault("subject", PERSON)
    return verify_view(headers, config=world.config, client_keys=world.resolver,
                       replay_store=store or MemoryJtiStore(), **kw)


def _refused(world, headers, **kw) -> str:
    with pytest.raises(ViewRefused) as info:
        _verify(world, headers, **kw)
    return info.value.code


def test_a_view_statement_verifies(world):
    view = _verify(world, _headers(_statement(world)))
    assert isinstance(view, View)
    assert (view.subject, view.uri, view.session, view.viewer, view.agent) == (
        PERSON, URI, "s-1", "user:7", "labsdata")
    assert view.audit()["actor"] == "view"


def test_any_page_of_the_resource_is_the_resource(world):
    assert _verify(world, _headers(_statement(world)), uri=URI + "?page=3&page_size=50").uri == URI


def test_the_presented_dpop_key_is_the_default_binding(world):
    token = presented_dpop_jkt.set(_jkt(world))
    try:
        view = verify_view(_headers(_statement(world)), config=world.config, client_keys=world.resolver,
                           replay_store=MemoryJtiStore(), uri=URI, subject=PERSON)
    finally:
        presented_dpop_jkt.reset(token)
    assert view.subject == PERSON


def test_no_statement_is_refused_never_the_person(world):
    assert _refused(world, {"Authorization": f"DPoP {TOKEN}"}) == "no_statement"


def test_two_statements_are_refused(world):
    raw = _statement(world)
    assert _refused(world, _headers(f"{raw},{raw}")) == "no_statement"


def test_an_agents_call_is_never_a_view(world):
    headers = _headers(_statement(world), **{contract.EXECUTION_HEADER: "an-agents-statement"})
    assert _refused(world, headers) == "agent_call"


def test_a_view_statement_is_spent_once(world):
    store = MemoryJtiStore()
    headers = _headers(_statement(world))
    _verify(world, headers, store=store)
    assert _refused(world, headers, store=store) == "replayed"


def test_a_statement_for_another_person(world):
    assert _refused(world, _headers(_statement(world, subject="43"))) == "wrong_person"
    assert _refused(world, _headers(_statement(world)), subject="") == "wrong_person"


@pytest.mark.parametrize("other", [
    "labs-result://another", "labs-result://abc1234", "other-scheme://abc123", "labs-result://abc123/x",
])
def test_a_statement_for_another_resource(world, other):
    assert _refused(world, _headers(_statement(world, uri=other))) == "wrong_resource"


def test_an_expired_statement(world):
    then = int(time.time()) - 3600
    assert _refused(world, _headers(_statement(world, iat=then, exp=then + 60))) == "expired"


def test_a_statement_living_too_long(world):
    now = int(time.time())
    assert _refused(world, _headers(_statement(world, iat=now, exp=now + 3600))) == "too_long"


def test_a_statement_for_another_host(world):
    assert _refused(world, _headers(_statement(world, audience="https://other.test/mcp/"))) == "bad_audience"


def test_a_statement_signed_by_another_key(world):
    from canopy_sdk.keys import generate_private_key

    assert _refused(world, _headers(_statement(world, key=generate_private_key("EdDSA")))) in (
        "bad_signature", "unknown_key")


def test_a_statement_from_another_issuer(world):
    assert _refused(world, _headers(_statement(world, iss="https://evil.test/oauth/client.json"))) == "bad_issuer"


def test_a_statement_not_made_for_a_view(world):
    assert _refused(world, _headers(_statement(world, actor="agent"))) == "bad_actor"


def test_a_statement_bound_to_another_dpop_key(world):
    assert _refused(world, _headers(_statement(world)), jkt="another-key") == "unbound"
    assert _refused(world, _headers(_statement(world)), jkt="") == "unbound"


def test_a_statement_for_another_access_token(world):
    assert _refused(world, _headers(_statement(world), token="a-different-token")) == "unbound"


def test_a_statement_without_its_token_hash(world):
    assert _refused(world, _headers(_statement(world, ath=None))) == "missing_claim"


def test_an_execution_statement_is_not_a_view_statement(world):
    raw = consumer.execution_statement(world.client.client_key, CLIENT_ID, RESOURCE,
                                       {"agent": "labsdata", "runner": {}, "requirements": [], "turn": "t"},
                                       jkt=_jkt(world), access_token=TOKEN)
    assert _refused(world, _headers(raw)) == "bad_typ"


def test_canopy_will_not_sign_a_view_statement_missing_a_binding(world):
    for missing in ("subject", "uri", "session", "access_token"):
        kw = {"subject": PERSON, "uri": URI, "session": "s-1", "access_token": TOKEN, missing: ""}
        with pytest.raises(ValueError):
            consumer.view_statement(world.client.client_key, CLIENT_ID, RESOURCE, jkt=_jkt(world), **kw)


def test_same_resource():
    assert same_resource("labs-result://a", "LABS-RESULT://a?page=2")
    assert not same_resource("labs-result://a", "labs-result://b")
    assert not same_resource("", "labs-result://a")


# --- the statement rides ONE request ----------------------------------------------------------


def test_a_view_statement_is_bound_to_its_request(world):
    read = ("resources/read", {"uri": URI + "?page_size=5&page=2"})
    raw = _statement(world, rpc=contract.rpc_digest(*read))
    # The host rebuilt the URI with its query in another order: the same request.
    assert _verify(world, _headers(raw), rpc=("resources/read", {"uri": URI + "?page=2&page_size=5"})).uri == URI
    assert _refused(world, _headers(_statement(world, rpc=contract.rpc_digest(*read))),
                    rpc=("resources/read", {"uri": URI + "?page=3&page_size=5"})) == "wrong_request"
    assert _refused(world, _headers(_statement(world, rpc=contract.rpc_digest(*read))),
                    rpc=("tools/call", {"name": "data_query", "arguments": {}})) == "wrong_request"
    # A statement with no request binding does not pass a host that asks for one.
    assert _refused(world, _headers(_statement(world)), rpc=read) == "wrong_request"


def test_rpc_digest_covers_the_call_not_its_envelope():
    call = {"name": "data_query", "arguments": {"sql": "x", "opportunity_ids": [1]}}
    same = {"arguments": {"opportunity_ids": [1], "sql": "x"}, "name": "data_query", "_meta": {"progressToken": 3}}
    assert contract.rpc_digest("tools/call", call) == contract.rpc_digest("tools/call", same)
    assert contract.rpc_digest("tools/call", call) != contract.rpc_digest(
        "tools/call", {**call, "arguments": {"sql": "y", "opportunity_ids": [1]}})
    body = b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":' + __import__("json").dumps(call).encode() + b"}"
    assert contract.rpc_digest_of_body(body) == contract.rpc_digest("tools/call", call)
    assert contract.rpc_of_body(body) == ("tools/call", call)
    assert contract.rpc_digest_of_body(b"") is None and contract.rpc_digest_of_body(b"[1]") is None


def test_a_read_digest_is_unambiguous_and_never_raises():
    def d(uri):
        return contract.rpc_digest("resources/read", {"uri": uri})

    # A value holding '&' is not a second pair; a value holding '=' is not a key.
    assert d(URI + "?a=%26b%3D1") != d(URI + "?a=&b=1")
    assert d(URI + "?a%3Db=c") != d(URI + "?a=b%3Dc")
    # Bytes that are not UTF-8 stay distinct; escaping that only re-spells a character does not matter.
    assert d(URI + "?a=%ff") != d(URI + "?a=%fe")
    assert d(URI + "?a=%C3%A9") == d(URI + "?a=é") and d(URI + "?a=%41") == d(URI + "?a=A")
    # A lone surrogate is hashed, not raised on, and meets nothing else.
    assert d(URI + "?a=\ud800") != d(URI + "?a=%ED%A0%80")
    assert contract.rpc_digest("tools/call", {"name": "t", "arguments": {"q": "\ud800"}})


def test_a_verifier_accepts_the_digest_canopy_signed_before_0_22():
    """For one deploy window: a host on 0.22 verifies a canopy that has not moved yet."""
    read = ("resources/read", {"uri": URI + "?a=%26b"})
    legacy = contract._legacy_rpc_digest(*read)
    assert legacy != contract.rpc_digest(*read)
    assert contract.rpc_matches(legacy, *read) and contract.rpc_matches(contract.rpc_digest(*read), *read)
    assert not contract.rpc_matches(legacy, "resources/read", {"uri": URI + "?a=c"})
    assert not contract.rpc_matches("", *read)
    # The forms agree wherever a query needs no escaping, a Labs page read among them.
    page = ("resources/read", {"uri": URI + "?page=2&page_size=100"})
    assert contract._legacy_rpc_digest(*page) == contract.rpc_digest(*page)
