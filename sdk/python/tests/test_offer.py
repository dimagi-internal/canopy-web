"""The site offer (``canopy_sdk.offer``) and the site's needs (``canopy_sdk.site``):
both halves round-trip, and every refusal fails closed."""
from __future__ import annotations

import time

import jwt
import pytest

from canopy_sdk import offer as offer_mod
from canopy_sdk import site as site_mod
from canopy_sdk.fetch import FetchError
from canopy_sdk.keys import generate_private_key, public_pem

from .helpers import CANOPY, CLIENT_ID, World

SITE = "connect-labs"
URL = offer_mod.offer_url(CANOPY)

CONTENT = {
    "site": {"name": SITE, "tenant": "connect"},
    "runner_flags": [{"flag": "zdr", "meaning": "zero data retention"}],
    "agents": [{
        "slug": "labsdata", "name": "labsdata", "description": "data questions",
        "workspace": "connect", "owner": "Jo", "url": "https://canopy.example/w/connect/agents/labsdata",
        "capabilities": [{"name": "data", "description": "ask", "callers": ["contact", "member"],
                          "phi_safe": True, "phi_set_by": "Jo", "phi_set_at": "2026-10-22T22:00:09Z"}],
        "runners": {"flags_available": ["zdr"], "ready_with": {"zdr": True}},
        "ready": True, "problems": [],
    }, {
        "slug": "ace", "name": "ACE", "description": "", "workspace": "connect", "owner": "Jo",
        "url": "", "capabilities": [], "runners": {"flags_available": [], "ready_with": {"zdr": False}},
        "ready": False, "problems": ["ACE has no capability naming connect-labs"],
    }],
}


@pytest.fixture
def world():
    return World()


def _served(world, token):
    """A `get` that stands in for canopy: checks the request, answers `token`."""
    seen = {}

    def get(url, *, headers, accept):
        seen.update(url=url, headers=headers, accept=accept)
        bearer = headers["Authorization"].removeprefix("Bearer ")
        offer_mod.verify_request(bearer, [world.host_key.public_key()], audiences=[URL])
        return token(bearer) if callable(token) else token

    return get, seen


# --- the request -----------------------------------------------------------------


def test_request_round_trips_and_spends_its_jti(world):
    token = offer_mod.request_assertion(SITE, world.host_key, URL, agent="labsdata")
    spent = []
    claims = offer_mod.verify_request(token, [public_pem(world.host_key)], audiences=[URL],
                                      spend_jti=spent.append)
    assert claims["iss"] == claims["sub"] == SITE
    assert claims["exp"] - claims["iat"] == 60
    assert claims["canopy_agent"] == "labsdata"
    assert spent == [claims]


@pytest.mark.parametrize("mutate,code", [
    (lambda c: c.update(aud="https://elsewhere.example/api/sites/offer"), "wrong_audience"),
    (lambda c: c.update(exp=c["iat"] + 600), "too_long"),
    (lambda c: c.update(iat=int(time.time()) - 400, exp=int(time.time()) - 340), "expired"),
    (lambda c: c.update(sub="someone-else"), "bad_subject"),
    (lambda c: c.pop("jti"), "incomplete"),
])
def test_request_refusals(world, mutate, code):
    now = int(time.time())
    claims = {"iss": SITE, "sub": SITE, "aud": URL, "iat": now, "exp": now + 60, "jti": "j"}
    mutate(claims)
    token = jwt.encode(claims, world.host_key, algorithm="EdDSA",
                       headers={"typ": offer_mod.REQUEST_TYP})
    with pytest.raises(offer_mod.OfferError) as exc:
        offer_mod.verify_request(token, [world.host_key.public_key()], audiences=[URL])
    assert exc.value.code == code


def test_request_signed_by_another_key_is_refused(world):
    token = offer_mod.request_assertion(SITE, generate_private_key("EdDSA"), URL)
    with pytest.raises(offer_mod.OfferError) as exc:
        offer_mod.verify_request(token, [world.host_key.public_key()], audiences=[URL])
    assert exc.value.code == "bad_signature"


def test_a_visitor_assertion_is_not_an_offer_request(world):
    from canopy_sdk.host import sign_visitor_assertion

    token = sign_visitor_assertion(world.config, "42")
    with pytest.raises(offer_mod.OfferError) as exc:
        offer_mod.verify_request(token, [world.host_key.public_key()], audiences=[URL])
    assert exc.value.code == "bad_typ"


# --- the offer -------------------------------------------------------------------


def test_offer_round_trips(world):
    token = offer_mod.sign_offer(world.client.client_key, CLIENT_ID, SITE, CONTENT)
    got = offer_mod.verify_offer(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver)
    assert got.site_name == SITE and got.tenant == "connect"
    assert got.slugs == ("labsdata", "ace")
    data = got.agent("labsdata")
    assert data.phi_safe and data.ready and data.ready_with_flag("zdr")
    assert data.capability("data").phi_set_by == "Jo"
    assert data.flags_available == ("zdr",)
    ace = got.agent("ace")
    assert not ace.ready and not ace.phi_safe and ace.problems
    assert got.flag("zdr").meaning == "zero data retention"
    assert got.agent("nobody") is None
    assert got.version == offer_mod.content_version(CONTENT)
    assert not got.stale


def test_version_changes_iff_content_changes():
    v = offer_mod.content_version(CONTENT)
    reordered = {"agents": CONTENT["agents"], "runner_flags": CONTENT["runner_flags"],
                 "site": dict(reversed(list(CONTENT["site"].items())))}
    assert offer_mod.content_version(reordered) == v
    assert offer_mod.content_version({**CONTENT, "iat": 1, "exp": 2, "v": 1}) == v
    changed = {**CONTENT, "agents": [{**CONTENT["agents"][0], "ready": False}, CONTENT["agents"][1]]}
    assert offer_mod.content_version(changed) != v


def test_offer_signed_by_another_key_is_refused(world):
    token = offer_mod.sign_offer(generate_private_key("EdDSA"), CLIENT_ID, SITE, CONTENT)
    with pytest.raises(offer_mod.OfferError) as exc:
        offer_mod.verify_offer(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver)
    assert exc.value.code == "unknown_key"


def test_offer_for_another_site_is_refused(world):
    token = offer_mod.sign_offer(world.client.client_key, CLIENT_ID, "another-site", CONTENT)
    with pytest.raises(offer_mod.OfferError) as exc:
        offer_mod.verify_offer(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver)
    assert exc.value.code == "bad_audience"


def test_expired_offer_is_refused(world):
    token = offer_mod.sign_offer(world.client.client_key, CLIENT_ID, SITE, CONTENT,
                                 now=time.time() - 2000)
    with pytest.raises(offer_mod.OfferError) as exc:
        offer_mod.verify_offer(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver)
    assert exc.value.code == "expired"


def _resign(world, mutate):
    token = offer_mod.sign_offer(world.client.client_key, CLIENT_ID, SITE, CONTENT)
    claims = jwt.decode(token, options={"verify_signature": False})
    mutate(claims)
    return jwt.encode(claims, world.client.client_key, algorithm="EdDSA",
                      headers={"kid": world.client_jwk["kid"], "typ": offer_mod.OFFER_TYP})


@pytest.mark.parametrize("mutate,code", [
    (lambda c: c.update(v=2), "bad_version"),
    (lambda c: c["agents"][0].update(ready=False), "bad_content_version"),
    (lambda c: c.update(iss="https://other.example/oauth/client.json"), "bad_issuer"),
    (lambda c: c.update(exp=c["iat"] + 86400), "too_long"),
])
def test_offer_refusals(world, mutate, code):
    with pytest.raises(offer_mod.OfferError) as exc:
        offer_mod.verify_offer(_resign(world, mutate), client_id=CLIENT_ID, site_name=SITE,
                               client_keys=world.resolver)
    assert exc.value.code == code


def test_execution_statement_is_not_an_offer(world):
    from canopy_sdk import consumer

    token = consumer.execution_statement(world.client.client_key, CLIENT_ID, SITE, {"agents": []},
                                         jkt="x")
    with pytest.raises(offer_mod.OfferError) as exc:
        offer_mod.verify_offer(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver)
    assert exc.value.code == "bad_typ"


def test_fetch_offer_asks_signed_and_verifies(world):
    token = offer_mod.sign_offer(world.client.client_key, CLIENT_ID, SITE, CONTENT)
    get, seen = _served(world, token)
    got = offer_mod.fetch_offer(CANOPY, SITE, world.host_key, world.resolver, get=get)
    assert got.agent("labsdata").ready
    assert seen["url"] == URL and seen["accept"] == "application/jose"


def test_fetch_offer_fails_closed_when_unreachable(world):
    def get(url, **kw):
        raise FetchError(f"{url} answered 401: unknown_issuer")

    with pytest.raises(offer_mod.OfferError) as exc:
        offer_mod.fetch_offer(CANOPY, SITE, world.host_key, world.resolver, get=get)
    assert exc.value.code == "unreachable" and "unknown_issuer" in exc.value.message


def test_fetch_offer_refuses_a_forged_answer(world):
    forged = offer_mod.sign_offer(generate_private_key("EdDSA"), CLIENT_ID, SITE, CONTENT)
    get, _ = _served(world, forged)
    with pytest.raises(offer_mod.OfferError):
        offer_mod.fetch_offer(CANOPY, SITE, world.host_key, world.resolver, get=get)


# --- site.json -------------------------------------------------------------------


PAGES = [{"key": "labs:workflow:run", "title": "A workflow run",
          "scopes": ["workflow:read", "workflow:act", "data:read"]}]
NEEDS = {"real_data": {"requires": {"phi_safe": True, "runner_flag": "zdr"}},
         "uses": {"agents": ["ace", "labsdata"], "real_data_agents": ["labsdata"], "version": "abc"}}


def test_site_document_round_trips(world):
    doc = site_mod.site_document(SITE, pages=PAGES, **NEEDS)
    token = site_mod.sign_site_document(world.host_key, doc)
    needs = site_mod.verify_site_document(token, [world.host_key.public_key()], site_name=SITE)
    assert needs.pages[0].scopes == ("workflow:read", "workflow:act", "data:read")
    assert needs.real_data_requires == {"phi_safe": True, "runner_flag": "zdr"}
    assert needs.uses_real_data_agents == ("labsdata",) and needs.uses_version == "abc"


def test_site_document_refusals(world):
    token = site_mod.sign_site_document(world.host_key, site_mod.site_document(SITE, pages=PAGES))
    with pytest.raises(site_mod.SiteDocumentError) as exc:
        site_mod.verify_site_document(token, [generate_private_key("EdDSA").public_key()], site_name=SITE)
    assert exc.value.code == "bad_signature"
    with pytest.raises(site_mod.SiteDocumentError) as exc:
        site_mod.verify_site_document(token, [world.host_key.public_key()], site_name="other")
    assert exc.value.code == "bad_issuer"
    with pytest.raises(site_mod.SiteDocumentError) as exc:
        site_mod.site_document(SITE, real_data={"requires": {"runner_flag": "made-up"}})
    assert exc.value.code == "malformed"


def test_site_document_url_sits_beside_the_jwks():
    assert (site_mod.site_document_url("https://labs.example.org/canopy/jwks.json")
            == "https://labs.example.org/canopy/site.json")
    assert site_mod.site_document_url("https://labs.example.org/keys") == ""
    assert site_mod.site_document_url("") == ""
