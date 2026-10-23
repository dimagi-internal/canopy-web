"""The site agents API (`GET /api/sites/agents`) and what the Connected sites page reads
back from the site (`…/connected-apps/{id}/needs`).

The API is driven over HTTP with a request signed the way a site's SDK signs
it, and its answer is verified with the SDK's own verifier against canopy's
published client keys — so a pass means a real site would believe it.
"""
from __future__ import annotations

import time

import jwt
import pytest
from canopy_sdk import site_agents as wire
from canopy_sdk import site as site_doc
from canopy_sdk.host import ClientKeyResolver
from canopy_sdk.keys import generate_private_key, public_jwk, public_pem
from canopy_sdk.stores import MemoryCache
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAssignment, RunnerFlag
from apps.host_grants import client_identity, site_needs
from apps.tokens.models import AppCredential, AppCredentialAgent
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.testing import enrolled

pytestmark = pytest.mark.django_db

SITE = "connect-labs"
PHI_IFACE = {"capabilities": {"data": {
    "description": "Ask data questions about the workflow you are looking at",
    "callers": ["contact", "member"], "sites": [SITE], "data": "phi",
    "tools": ["mcp__*canopy-web__who_is_asking"]}}}
PLAIN_IFACE = {"capabilities": {"connect": {
    "callers": ["contact"], "sites": [SITE], "tools": ["mcp__*canopy-web__who_is_asking"]}}}


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def w():
    owner = get_user_model().objects.create_user("jo", "jo@dimagi.com", "pw", first_name="Jo",
                                                 last_name="Owner")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    labsdata = Agent.objects.create(slug="labsdata", name="labsdata", workspace=ws, owner=owner,
                                    description="data questions", interface=parse(PHI_IFACE),
                                    interface_published_by=owner,
                                    interface_published_at=timezone.now())
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner,
                               interface=parse(PLAIN_IFACE))
    bare = Agent.objects.create(slug="bare", name="Bare", workspace=ws, owner=owner)
    Agent.objects.create(slug="unlisted", name="Unlisted", workspace=ws, owner=owner,
                         interface=parse(PLAIN_IFACE))
    key = generate_private_key("EdDSA")
    app = AppCredential.create_credential(name=SITE, created_by=owner, workspace=ws)
    app.public_keys = [public_pem(key)]
    app.save()
    for agent in (labsdata, ace, bare):
        AppCredentialAgent.objects.create(app=app, agent=agent)
    return {"owner": owner, "ws": ws, "labsdata": labsdata, "ace": ace, "bare": bare,
            "app": app, "key": key}


def _runner(w, *agents, zdr=False, online=True, name="jo-mbp"):
    runner = enrolled(Runner.objects.create(
        name=name, kind=Runner.EMDASH, host=name, owner=w["owner"],
        status=Runner.ONLINE if online else Runner.DISCONNECTED, ready=True,
        last_heartbeat_at=timezone.now() if online else None,
        capabilities={"data_mode": "zdr"} if zdr else {}))
    if zdr:
        RunnerFlag.objects.create(runner=runner, flag="zdr", declared_by=w["owner"])
    for agent in agents:
        RunnerAssignment.objects.create(agent=agent, runner=runner, rank=1, enabled=True)
    return runner


def _url():
    return wire.endpoint_url(client_identity.public_base())


def _ask(w, *, key=None, token=None, **kw):
    token = token or wire.request_assertion(SITE, key or w["key"], _url(), **kw)
    return Client().get("/api/sites/agents", HTTP_AUTHORIZATION=f"Bearer {token}")


def _canopy_keys():
    docs = {client_identity.client_id(): client_identity.client_metadata(),
            client_identity.jwks_uri(): client_identity.published_jwks()}
    return ClientKeyResolver(fetch_json=docs.__getitem__, cache=MemoryCache())


def _agents(w, **kw) -> wire.SiteAgents:
    response = _ask(w, **kw)
    assert response.status_code == 200, response.content
    assert response["Content-Type"] == "application/jose"
    return wire.verify_site_agents(response.content.decode(), client_id=client_identity.client_id(),
                             site_name=SITE, client_keys=_canopy_keys())


# --- who may ask ------------------------------------------------------------------


def test_a_request_signed_by_another_key_is_refused(w):
    response = _ask(w, key=generate_private_key("EdDSA"))
    assert response.status_code == 401
    assert "bad_signature" in response.json()["detail"]


def test_an_unknown_site_is_refused_like_any_other(w):
    token = wire.request_assertion("not-a-site", w["key"], _url())
    response = _ask(w, token=token)
    assert response.status_code == 401 and "unknown_issuer" in response.json()["detail"]


def test_a_disconnected_site_is_unknown(w):
    AppCredential.objects.filter(pk=w["app"].pk).update(revoked_at=timezone.now())
    assert _ask(w).status_code == 401


def test_no_bearer_is_refused():
    assert Client().get("/api/sites/agents").status_code == 401


def test_a_request_is_single_use(w):
    token = wire.request_assertion(SITE, w["key"], _url())
    assert _ask(w, token=token).status_code == 200
    again = _ask(w, token=token)
    assert again.status_code == 401 and "replayed" in again.json()["detail"]


def test_a_request_for_another_canopy_is_refused(w):
    token = wire.request_assertion(SITE, w["key"], "https://other-canopy.example/api/sites/agents")
    response = _ask(w, token=token)
    assert response.status_code == 401 and "wrong_audience" in response.json()["detail"]


def test_a_visitor_assertion_cannot_buy_the_list(w):
    now = int(time.time())
    token = jwt.encode({"iss": SITE, "sub": "u-1", "aud": _url(), "iat": now, "exp": now + 60,
                        "jti": "x"}, w["key"], algorithm="EdDSA",
                       headers={"kid": public_jwk(w["key"])["kid"]})
    response = _ask(w, token=token)
    assert response.status_code == 401 and "bad_typ" in response.json()["detail"]


def test_two_tenants_sharing_a_name_are_told_apart_by_the_signed_agent(w):
    other_owner = get_user_model().objects.create_user("x", "x@dimagi.com", "pw")
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=other_owner)
    AppCredential.create_credential(name=SITE, created_by=other_owner, workspace=other)
    assert _ask(w).status_code == 409
    assert _agents(w, agent="labsdata").tenant == "connect"


# --- what it says -----------------------------------------------------------------


def test_it_lists_only_the_agents_allowed_on_the_site(w):
    got = _agents(w)
    assert got.slugs == ("ace", "bare", "labsdata")       # not "unlisted"
    assert got.site_name == SITE and got.tenant == "connect"
    bare = got.agent("bare")
    assert not bare.ready and not bare.capabilities
    assert any("no capability naming connect-labs" in p for p in bare.problems)


def test_an_agent_of_another_workspace_is_never_listed(w):
    stranger = get_user_model().objects.create_user("s", "s@dimagi.com", "pw")
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=stranger)
    foreign = Agent.objects.create(slug="foreign", name="Foreign", workspace=other,
                                   interface=parse(PLAIN_IFACE))
    AppCredentialAgent.objects.create(app=w["app"], agent=foreign)
    assert "foreign" not in _agents(w).slugs


def test_phi_safe_reflects_the_interface_setting(w):
    got = _agents(w)
    data = got.agent("labsdata").capability("data")
    assert data.phi_safe and data.phi_set_by == "Jo Owner" and data.phi_set_at
    assert data.callers == ("contact", "member")
    assert not got.agent("ace").phi_safe
    assert got.agent("ace").capability("connect").phi_set_by == ""


def test_runners_and_readiness_reflect_the_fleet(w):
    got = _agents(w)
    data = got.agent("labsdata")
    assert not data.ready and data.flags_available == () and not data.ready_with_flag("zdr")
    assert any("no runner takes" in p and "#running" in p for p in data.problems)

    _runner(w, w["labsdata"], w["ace"])
    got = _agents(w)
    data = got.agent("labsdata")
    assert data.ready and not data.ready_with_flag("zdr")
    assert any(p.startswith("no ready runner with zdr") for p in data.problems)
    assert got.agent("ace").ready and got.agent("ace").problems == ()

    _runner(w, w["labsdata"], zdr=True, name="zdr-box")
    data = _agents(w).agent("labsdata")
    assert data.flags_available == ("zdr",) and data.ready_with_flag("zdr") and data.problems == ()


def test_a_declared_flag_the_box_contradicts_is_not_available(w):
    runner = _runner(w, w["labsdata"], zdr=True)
    Runner.objects.filter(pk=runner.pk).update(capabilities={"data_mode": "standard"})
    data = _agents(w).agent("labsdata")
    assert data.flags_available == () and not data.ready_with_flag("zdr")


def test_an_offline_runner_is_listed_but_not_ready(w):
    _runner(w, w["labsdata"], zdr=True, online=False)
    data = _agents(w).agent("labsdata")
    assert data.flags_available == ("zdr",) and not data.ready_with_flag("zdr") and not data.ready
    assert any("is ready now" in p for p in data.problems)


def test_runner_flags_are_every_known_flag_with_its_meaning(w):
    from canopy_sdk import contract

    flags = _agents(w).runner_flags
    assert {f.flag for f in flags} == set(contract.RUNNER_FLAGS)
    assert all(f.meaning for f in flags)


def test_version_changes_exactly_when_the_content_does(w):
    first, second = _agents(w), _agents(w)
    assert first.version == second.version and first.issued_at <= second.issued_at
    _runner(w, w["ace"])
    third = _agents(w)
    assert third.version != first.version
    AppCredentialAgent.objects.filter(app=w["app"], agent=w["bare"]).delete()
    assert _agents(w).version != third.version


def test_the_response_is_signed_by_the_execution_key(w):
    response = _ask(w)
    header = jwt.get_unverified_header(response.content.decode())
    assert header["typ"] == wire.RESPONSE_TYP
    assert header["kid"] == client_identity.published_jwks()["keys"][0]["kid"]


# --- the site's needs, on the Connected sites page -------------------------------


NEEDS = {"real_data": {"requires": {"phi_safe": True, "runner_flag": "zdr"}},
         "uses": {"agents": ["ace", "labsdata", "ghost"], "real_data_agents": ["labsdata"],
                  "version": "v-old"}}
PAGES = [{"key": "labs:workflow:run:data", "title": "A workflow run, read-only",
          "scopes": ["workflow:read", "data:read"]}]


def _publish(w, monkeypatch, *, key=None, fail=False):
    w["app"].jwks_url = "https://labs.example.org/canopy/jwks.json"
    w["app"].save()
    signer = key or w["key"]
    monkeypatch.setattr("apps.embedding.jwks._fetch",
                        lambda url: [{**public_jwk(w["key"]), "use": "sig"}])
    calls = []

    def get(url):
        calls.append(url)
        if fail:
            from canopy_sdk.fetch import FetchError

            raise FetchError(f"{url} answered 503")
        return site_doc.sign_site_document(signer, site_doc.site_document(SITE, pages=PAGES, **NEEDS))

    monkeypatch.setattr(site_needs, "_get", get)
    return calls


def _needs(w):
    client = Client()
    client.force_login(w["owner"])
    response = client.get(f"/api/workspaces/connect/connected-apps/{w['app'].pk}/needs")
    assert response.status_code == 200, response.content
    return response.json()


def test_the_page_shows_the_sites_needs_and_who_meets_them(w, monkeypatch):
    calls = _publish(w, monkeypatch)
    _runner(w, w["labsdata"], w["ace"], zdr=True)
    body = _needs(w)
    assert calls == ["https://labs.example.org/canopy/site.json"]
    assert body["available"] and not body["stale"]
    assert body["pages"] == PAGES
    assert body["requires"] == {"phi_safe": True, "runner_flag": "zdr"}
    rows = {r["slug"]: r for r in body["agents"]}
    assert rows["labsdata"]["offered"] and rows["labsdata"]["real_data"]
    assert rows["labsdata"]["meets_real_data"] and rows["labsdata"]["why_not"] == []
    assert rows["ace"]["offered"] and not rows["ace"]["meets_real_data"]
    assert any("not PHI-safe" in why for why in rows["ace"]["why_not"])
    assert not rows["bare"]["offered"] and not rows["bare"]["ready"]
    assert body["not_allowed"] == ["ghost"]
    assert body["uses_version"] == "v-old" and body["site_agents_version"] != "v-old"
    _needs(w)
    assert len(calls) == 1, "a good copy is cached"


def test_without_a_zdr_runner_the_real_data_agent_falls_short_and_says_why(w, monkeypatch):
    _publish(w, monkeypatch)
    _runner(w, w["labsdata"])
    row = next(r for r in _needs(w)["agents"] if r["slug"] == "labsdata")
    assert not row["meets_real_data"]
    assert any(why.startswith("no ready runner with zdr") and "#running" in why
               for why in row["why_not"])


def test_a_site_json_signed_by_another_key_is_unavailable(w, monkeypatch):
    _publish(w, monkeypatch, key=generate_private_key("EdDSA"))
    body = _needs(w)
    assert not body["available"] and "bad_signature" in body["reason"]
    assert body["agents"] == []


def test_an_unreachable_site_json_fails_closed(w, monkeypatch):
    _publish(w, monkeypatch, fail=True)
    body = _needs(w)
    assert not body["available"] and "unreachable" in body["reason"]


def test_a_failed_refetch_serves_the_last_good_copy_marked_stale(w, monkeypatch):
    _publish(w, monkeypatch)
    _needs(w)
    cache.delete_many(_fresh_keys(w))                      # the fresh copy expires
    _publish(w, monkeypatch, fail=True)
    body = _needs(w)
    assert body["available"] and body["stale"] and "unreachable" in body["reason"]


def _fresh_keys(w):
    url = site_needs.document_url(w["app"].jwks_url)
    w["app"].refresh_from_db()
    return [site_needs._keys(w["app"], url)[0]]


def test_site_json_sits_beside_the_keys_wherever_they_are():
    assert (site_needs.document_url("https://labs.example.org/canopy/jwks.json")
            == "https://labs.example.org/canopy/site.json")
    assert (site_needs.document_url("https://labs.connect.dimagi.com/labs/canopy/jwks/")
            == "https://labs.connect.dimagi.com/labs/canopy/site.json")
    assert site_needs.document_url("https://labs.example.org/keys") == ""


def test_no_jwks_url_means_nowhere_to_read(w):
    body = _needs(w)
    assert not body["available"] and body["url"] == "" and "jwks.json" in body["reason"]


def test_a_non_member_cannot_read_a_sites_needs(w):
    stranger = get_user_model().objects.create_user("s", "s@dimagi.com", "pw")
    client = Client()
    client.force_login(stranger)
    response = client.get(f"/api/workspaces/connect/connected-apps/{w['app'].pk}/needs")
    assert response.status_code == 404
