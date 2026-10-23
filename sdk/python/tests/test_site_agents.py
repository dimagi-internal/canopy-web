"""The site agents API (``canopy_sdk.site_agents``) and the site's needs (``canopy_sdk.site``):
both halves round-trip, and every refusal fails closed."""
from __future__ import annotations

import time

import jwt
import pytest

from canopy_sdk import site_agents as wire
from canopy_sdk import site as site_mod
from canopy_sdk.fetch import FetchError
from canopy_sdk.keys import generate_private_key, public_pem

from .helpers import CANOPY, CLIENT_ID, World

SITE = "connect-labs"
URL = wire.endpoint_url(CANOPY)

CONTENT = {
    "site": {"name": SITE, "tenant": "connect"},
    "runner_flags": [{"flag": "sensitive_data", "meaning": "handles sensitive data"}],
    "agents": [{
        "slug": "labsdata", "name": "labsdata", "description": "data questions",
        "workspace": "connect", "owner": "Jo", "url": "https://canopy.example/w/connect/agents/labsdata",
        "capabilities": [{"name": "data", "description": "ask", "callers": ["contact", "member"],
                          "handles_sensitive": True, "sensitive_set_by": "Jo", "sensitive_set_at": "2026-10-22T22:00:09Z"}],
        "runners": {"flags_available": ["sensitive_data"], "ready_with": {"sensitive_data": True}},
        "ready": True, "problems": [],
    }, {
        "slug": "ace", "name": "ACE", "description": "", "workspace": "connect", "owner": "Jo",
        "url": "", "capabilities": [], "runners": {"flags_available": [], "ready_with": {"sensitive_data": False}},
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
        wire.verify_request(bearer, [world.host_key.public_key()], audiences=[URL])
        return token(bearer) if callable(token) else token

    return get, seen


# --- the request -----------------------------------------------------------------


def test_request_round_trips_and_spends_its_jti(world):
    token = wire.request_assertion(SITE, world.host_key, URL, agent="labsdata")
    spent = []
    claims = wire.verify_request(token, [public_pem(world.host_key)], audiences=[URL],
                                      spend_jti=spent.append)
    assert claims["iss"] == claims["sub"] == SITE
    assert claims["exp"] - claims["iat"] == 60
    assert claims["canopy_agent"] == "labsdata"
    assert spent == [claims]


@pytest.mark.parametrize("mutate,code", [
    (lambda c: c.update(aud="https://elsewhere.example/api/sites/agents"), "wrong_audience"),
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
                       headers={"typ": wire.REQUEST_TYP})
    with pytest.raises(wire.SiteAgentsError) as exc:
        wire.verify_request(token, [world.host_key.public_key()], audiences=[URL])
    assert exc.value.code == code


def test_request_signed_by_another_key_is_refused(world):
    token = wire.request_assertion(SITE, generate_private_key("EdDSA"), URL)
    with pytest.raises(wire.SiteAgentsError) as exc:
        wire.verify_request(token, [world.host_key.public_key()], audiences=[URL])
    assert exc.value.code == "bad_signature"


def test_a_visitor_assertion_is_not_a_site_agents_request(world):
    from canopy_sdk.host import sign_visitor_assertion

    token = sign_visitor_assertion(world.config, "42")
    with pytest.raises(wire.SiteAgentsError) as exc:
        wire.verify_request(token, [world.host_key.public_key()], audiences=[URL])
    assert exc.value.code == "bad_typ"


# --- the response ----------------------------------------------------------------


def test_response_round_trips(world):
    token = wire.sign_site_agents(world.client.client_key, CLIENT_ID, SITE, CONTENT)
    got = wire.verify_site_agents(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver)
    assert got.site_name == SITE and got.tenant == "connect"
    assert got.slugs == ("labsdata", "ace")
    data = got.agent("labsdata")
    assert data.handles_sensitive and data.ready and data.ready_with_flag("sensitive_data")
    assert data.capability("data").sensitive_set_by == "Jo"
    assert data.flags_available == ("sensitive_data",)
    ace = got.agent("ace")
    assert not ace.ready and not ace.handles_sensitive and ace.problems
    assert got.flag("sensitive_data").meaning == "handles sensitive data"
    assert got.agent("nobody") is None
    assert got.version == wire.content_version(CONTENT)
    assert not got.stale


def test_version_changes_iff_content_changes():
    v = wire.content_version(CONTENT)
    reordered = {"agents": CONTENT["agents"], "runner_flags": CONTENT["runner_flags"],
                 "site": dict(reversed(list(CONTENT["site"].items())))}
    assert wire.content_version(reordered) == v
    assert wire.content_version({**CONTENT, "iat": 1, "exp": 2, "v": 1}) == v
    changed = {**CONTENT, "agents": [{**CONTENT["agents"][0], "ready": False}, CONTENT["agents"][1]]}
    assert wire.content_version(changed) != v


def test_response_signed_by_another_key_is_refused(world):
    token = wire.sign_site_agents(generate_private_key("EdDSA"), CLIENT_ID, SITE, CONTENT)
    with pytest.raises(wire.SiteAgentsError) as exc:
        wire.verify_site_agents(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver)
    assert exc.value.code == "unknown_key"


def test_response_for_another_site_is_refused(world):
    token = wire.sign_site_agents(world.client.client_key, CLIENT_ID, "another-site", CONTENT)
    with pytest.raises(wire.SiteAgentsError) as exc:
        wire.verify_site_agents(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver)
    assert exc.value.code == "bad_audience"


def test_expired_response_is_refused(world):
    token = wire.sign_site_agents(world.client.client_key, CLIENT_ID, SITE, CONTENT,
                                 now=time.time() - 2000)
    with pytest.raises(wire.SiteAgentsError) as exc:
        wire.verify_site_agents(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver)
    assert exc.value.code == "expired"


def _resign(world, mutate):
    token = wire.sign_site_agents(world.client.client_key, CLIENT_ID, SITE, CONTENT)
    claims = jwt.decode(token, options={"verify_signature": False})
    mutate(claims)
    return jwt.encode(claims, world.client.client_key, algorithm="EdDSA",
                      headers={"kid": world.client_jwk["kid"], "typ": wire.RESPONSE_TYP})


@pytest.mark.parametrize("mutate,code", [
    (lambda c: c.update(v=2), "bad_version"),
    (lambda c: c["agents"][0].update(ready=False), "bad_content_version"),
    (lambda c: c.update(iss="https://other.example/oauth/client.json"), "bad_issuer"),
    (lambda c: c.update(exp=c["iat"] + 86400), "too_long"),
])
def test_response_refusals(world, mutate, code):
    with pytest.raises(wire.SiteAgentsError) as exc:
        wire.verify_site_agents(_resign(world, mutate), client_id=CLIENT_ID, site_name=SITE,
                               client_keys=world.resolver)
    assert exc.value.code == code


def test_execution_statement_is_not_a_site_agents_response(world):
    from canopy_sdk import consumer

    token = consumer.execution_statement(world.client.client_key, CLIENT_ID, SITE, {"agents": []},
                                         jkt="x")
    with pytest.raises(wire.SiteAgentsError) as exc:
        wire.verify_site_agents(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver)
    assert exc.value.code == "bad_typ"


def test_fetch_site_agents_asks_signed_and_verifies(world):
    token = wire.sign_site_agents(world.client.client_key, CLIENT_ID, SITE, CONTENT)
    get, seen = _served(world, token)
    got = wire.fetch_site_agents(CANOPY, SITE, world.host_key, world.resolver, get=get)
    assert got.agent("labsdata").ready
    assert seen["url"] == URL and seen["accept"] == "application/jose"


def test_fetch_site_agents_fails_closed_when_unreachable(world):
    def get(url, **kw):
        raise FetchError(f"{url} answered 401: unknown_issuer")

    with pytest.raises(wire.SiteAgentsError) as exc:
        wire.fetch_site_agents(CANOPY, SITE, world.host_key, world.resolver, get=get)
    assert exc.value.code == "unreachable" and "unknown_issuer" in exc.value.message


def test_fetch_site_agents_refuses_a_forged_answer(world):
    forged = wire.sign_site_agents(generate_private_key("EdDSA"), CLIENT_ID, SITE, CONTENT)
    get, _ = _served(world, forged)
    with pytest.raises(wire.SiteAgentsError):
        wire.fetch_site_agents(CANOPY, SITE, world.host_key, world.resolver, get=get)


# --- site.json -------------------------------------------------------------------


PAGES = [{"key": "labs:workflow:run", "title": "A workflow run",
          "scopes": ["workflow:read", "workflow:act", "data:read"]}]
NEEDS = {"real_data": {"requires": {"handles_sensitive": True, "runner_flag": "sensitive_data"}},
         "uses": {"agents": ["ace", "labsdata"], "real_data_agents": ["labsdata"], "version": "abc"}}


def test_site_document_round_trips(world):
    doc = site_mod.site_document(SITE, pages=PAGES, **NEEDS)
    token = site_mod.sign_site_document(world.host_key, doc)
    needs = site_mod.verify_site_document(token, [world.host_key.public_key()], site_name=SITE)
    assert needs.pages[0].scopes == ("workflow:read", "workflow:act", "data:read")
    assert needs.real_data_requires == {"handles_sensitive": True, "runner_flag": "sensitive_data"}
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


# --- naming the tenant, refusals, links (0.16) ---------------------------------------


def test_request_names_the_workspace_directly(world):
    token = wire.request_assertion(SITE, world.host_key, URL, workspace="connect")
    claims = wire.verify_request(token, [public_pem(world.host_key)], audiences=[URL])
    assert claims["canopy_workspace"] == "connect" and "canopy_agent" not in claims
    both = wire.verify_request(wire.request_assertion(SITE, world.host_key, URL, workspace="connect",
                                                      agent="labsdata"),
                               [public_pem(world.host_key)], audiences=[URL])
    assert (both["canopy_workspace"], both["canopy_agent"]) == ("connect", "labsdata")


def test_an_overlong_workspace_is_refused_before_signing(world):
    with pytest.raises(ValueError):
        wire.request_assertion(SITE, world.host_key, URL, workspace="w" * 65)


@pytest.mark.parametrize("detail,code", [
    ("unknown_issuer: no connected site named 'connect-labs'", "unknown_issuer"),
    ("ambiguous_issuer: more than one workspace has a site named 'connect-labs'", "ambiguous_issuer"),
    ("workspace_mismatch: agent 'ace' is not an agent of workspace 'connect'", "workspace_mismatch"),
    ("", "refused"),
    ("Something Else Went Wrong", "refused"),
])
def test_a_refusal_carries_canopys_code_not_unreachable(world, detail, code):
    def get(url, **kw):
        raise FetchError(f"{url} answered 409: {detail}", status=409, detail=detail)

    with pytest.raises(wire.SiteAgentsError) as exc:
        wire.fetch_site_agents(CANOPY, SITE, world.host_key, world.resolver, get=get)
    assert exc.value.code == code and exc.value.status == 409
    assert wire.SiteAgentsError.from_dict(exc.value.as_dict()).code == code


def test_fetch_site_agents_sends_the_workspace(world):
    token = wire.sign_site_agents(world.client.client_key, CLIENT_ID, SITE, CONTENT)
    get, seen = _served(world, token)
    wire.fetch_site_agents(CANOPY, SITE, world.host_key, world.resolver, workspace="connect", get=get)
    sent = jwt.decode(seen["headers"]["Authorization"].removeprefix("Bearer "),
                      options={"verify_signature": False})
    assert sent["canopy_workspace"] == "connect"


def test_the_answer_links_the_sites_page_and_each_problems_fix(world):
    content = {**CONTENT, "site": {**CONTENT["site"],
                                   "settings_url": "https://canopy.example/w/connect/settings/connected-apps#site-7"},
               "agents": [{**CONTENT["agents"][1], "problem_links": [
                   {"text": "ACE has no capability naming connect-labs",
                    "url": "https://canopy.example/w/connect/agents/ace/settings#reach"}]}]}
    token = wire.sign_site_agents(world.client.client_key, CLIENT_ID, SITE, content)
    got = wire.verify_site_agents(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver)
    assert got.site_settings_url.endswith("/settings/connected-apps#site-7")
    ace = got.agent("ace")
    assert ace.problem_links == (wire.AgentProblem("ACE has no capability naming connect-labs",
                                                   "https://canopy.example/w/connect/agents/ace/settings#reach"),)
    assert ace.problems == ("ACE has no capability naming connect-labs",)


def test_an_answer_from_an_older_canopy_has_no_links(world):
    token = wire.sign_site_agents(world.client.client_key, CLIENT_ID, SITE, CONTENT)
    got = wire.verify_site_agents(token, client_id=CLIENT_ID, site_name=SITE, client_keys=world.resolver)
    assert got.site_settings_url == "" and got.agent("ace").problem_links == ()


SITE_FACTS = {"origins": ["https://labs.example.org/", "https://labs.example.org"],
              "jwks_url": "https://labs.example.org/canopy/jwks.json",
              "issuer": "https://labs.example.org", "mcp_resource": "https://labs.example.org/mcp/"}


def test_site_document_declares_the_sites_own_facts(world):
    token = site_mod.sign_site_document(world.host_key, site_mod.site_document(SITE, pages=PAGES,
                                                                               site=SITE_FACTS))
    got = site_mod.verify_site_document(token, [world.host_key.public_key()], site_name=SITE)
    assert got.site == site_mod.SiteFacts(origins=("https://labs.example.org",),
                                          jwks_url="https://labs.example.org/canopy/jwks.json",
                                          issuer="https://labs.example.org",
                                          mcp_resource="https://labs.example.org/mcp/")


def test_a_document_without_a_site_block_has_none(world):
    token = site_mod.sign_site_document(world.host_key, site_mod.site_document(SITE, pages=PAGES))
    got = site_mod.verify_site_document(token, [world.host_key.public_key()], site_name=SITE)
    assert got.site is None and "site" not in got.claims


@pytest.mark.parametrize("site", [
    {"origins": ["https://labs.example.org/app"]},
    {"origins": "https://labs.example.org"},
    {"jwks_url": "ftp://labs.example.org/jwks.json"},
    {"issuer": 7},
    {"made_up": "x"},
])
def test_a_malformed_site_block_is_refused(site):
    with pytest.raises(site_mod.SiteDocumentError) as exc:
        site_mod.site_document(SITE, site=site)
    assert exc.value.code == "malformed"
