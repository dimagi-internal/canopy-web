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
from django.test import Client, override_settings
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
SENSITIVE_IFACE = {"capabilities": {"data": {
    "description": "Ask data questions about the workflow you are looking at",
    "callers": ["contact", "member"], "sites": [SITE], "data": "sensitive",
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
                                    description="data questions", interface=parse(SENSITIVE_IFACE),
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


def _runner(w, *agents, sensitive=False, online=True, name="jo-mbp"):
    runner = enrolled(Runner.objects.create(
        name=name, kind=Runner.EMDASH, host=name, owner=w["owner"],
        status=Runner.ONLINE if online else Runner.DISCONNECTED, ready=True,
        last_heartbeat_at=timezone.now() if online else None,
        capabilities={"data_mode": "sensitive_data"} if sensitive else {}))
    if sensitive:
        RunnerFlag.objects.create(runner=runner, flag="sensitive_data", declared_by=w["owner"])
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


@override_settings(REQUIRE_AUTH=True)
def test_a_site_reaches_the_endpoint_past_the_login_gate(w):
    """The site holds no canopy session or PAT: its signed request is the whole
    credential, so the login gate must let it reach the view (and refusals must
    be the view's own, not the gate's)."""
    assert _ask(w).status_code == 200
    refused = Client().get("/api/sites/agents")
    assert refused.status_code == 401 and "no_assertion" in refused.json()["detail"]


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


def test_handles_sensitive_reflects_the_interface_setting(w):
    got = _agents(w)
    data = got.agent("labsdata").capability("data")
    assert data.handles_sensitive and data.sensitive_set_by == "Jo Owner" and data.sensitive_set_at
    assert data.callers == ("contact", "member")
    assert not got.agent("ace").handles_sensitive
    assert got.agent("ace").capability("connect").sensitive_set_by == ""


def test_runners_and_readiness_reflect_the_fleet(w):
    got = _agents(w)
    data = got.agent("labsdata")
    assert not data.ready and data.flags_available == () and not data.ready_with_flag("sensitive_data")
    assert any("no runner takes" in p and "#running" in p for p in data.problems)

    _runner(w, w["labsdata"], w["ace"])
    got = _agents(w)
    data = got.agent("labsdata")
    assert data.ready and not data.ready_with_flag("sensitive_data")
    assert any(p.startswith("no ready runner with sensitive") for p in data.problems)
    assert got.agent("ace").ready and got.agent("ace").problems == ()

    _runner(w, w["labsdata"], sensitive=True, name="sensitive-box")
    data = _agents(w).agent("labsdata")
    assert data.flags_available == ("sensitive_data",) and data.ready_with_flag("sensitive_data") and data.problems == ()


def test_a_declared_flag_the_box_contradicts_is_not_available(w):
    runner = _runner(w, w["labsdata"], sensitive=True)
    Runner.objects.filter(pk=runner.pk).update(capabilities={"data_mode": "standard"})
    data = _agents(w).agent("labsdata")
    assert data.flags_available == () and not data.ready_with_flag("sensitive_data")


def test_an_offline_runner_is_listed_but_not_ready(w):
    _runner(w, w["labsdata"], sensitive=True, online=False)
    data = _agents(w).agent("labsdata")
    assert data.flags_available == ("sensitive_data",) and not data.ready_with_flag("sensitive_data") and not data.ready
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


NEEDS = {"real_data": {"requires": {"handles_sensitive": True, "runner_flag": "sensitive_data"}},
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
    _runner(w, w["labsdata"], w["ace"], sensitive=True)
    body = _needs(w)
    assert calls == ["https://labs.example.org/canopy/site.json"]
    assert body["available"] and not body["stale"]
    assert body["pages"] == PAGES
    assert body["requires"] == {"handles_sensitive": True, "runner_flag": "sensitive_data"}
    rows = {r["slug"]: r for r in body["agents"]}
    assert rows["labsdata"]["offered"] and rows["labsdata"]["real_data"]
    assert rows["labsdata"]["meets_real_data"] and rows["labsdata"]["why_not"] == []
    assert rows["ace"]["offered"] and not rows["ace"]["meets_real_data"]
    assert any("does not handle sensitive data" in why for why in rows["ace"]["why_not"])
    assert not rows["bare"]["offered"] and not rows["bare"]["ready"]
    assert body["not_allowed"] == ["ghost"]
    assert body["uses_version"] == "v-old" and body["site_agents_version"] != "v-old"
    _needs(w)
    assert len(calls) == 1, "a good copy is cached"


def test_without_a_sensitive_runner_the_real_data_agent_falls_short_and_says_why(w, monkeypatch):
    _publish(w, monkeypatch)
    _runner(w, w["labsdata"])
    row = next(r for r in _needs(w)["agents"] if r["slug"] == "labsdata")
    assert not row["meets_real_data"]
    assert any(why.startswith("no ready runner with sensitive") and "#running" in why
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


# --- naming the tenant by workspace ------------------------------------------------


def _second_tenant_with_the_same_name():
    other_owner = get_user_model().objects.create_user("x", "x@dimagi.com", "pw")
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=other_owner)
    Agent.objects.create(slug="other-agent", name="Other", workspace=other)
    AppCredential.create_credential(name=SITE, created_by=other_owner, workspace=other)


def test_the_signed_workspace_tells_two_tenants_apart_with_no_agent(w):
    _second_tenant_with_the_same_name()
    assert _ask(w).status_code == 409
    assert _agents(w, workspace="connect").tenant == "connect"


def test_the_workspace_keeps_working_when_the_agent_that_named_it_is_gone(w):
    """The audit's failure: a host named its tenant through its default agent,
    the agent was deleted, and the site agents API answered nothing. Naming the
    workspace does not depend on any one agent."""
    _second_tenant_with_the_same_name()
    AppCredentialAgent.objects.filter(agent__slug="ace").delete()
    Agent.objects.filter(slug="ace").delete()
    assert _ask(w, agent="ace").status_code == 401           # through the agent: nothing
    assert _agents(w, workspace="connect").slugs == ("bare", "labsdata")


def test_a_workspace_and_an_agent_that_disagree_are_refused_not_picked(w):
    _second_tenant_with_the_same_name()
    response = _ask(w, workspace="connect", agent="other-agent")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail.startswith("workspace_mismatch") and "'other'" not in detail, \
        "the refusal names what to fix, not where the agent really is"
    # A missing agent answers exactly as another workspace's: nothing to probe.
    missing = _ask(w, workspace="connect", agent="no-such-agent")
    assert missing.status_code == 409
    assert missing.json()["detail"] == detail.replace("other-agent", "no-such-agent")


def test_a_workspace_that_has_not_registered_the_site_is_unknown(w):
    _second_tenant_with_the_same_name()
    AppCredential.objects.filter(workspace_id="other").delete()
    response = _ask(w, workspace="other")
    assert response.status_code == 401 and "unknown_issuer" in response.json()["detail"]


# --- links a host renders ------------------------------------------------------------


def test_the_site_block_links_this_sites_page_in_canopy(w):
    got = _agents(w)
    assert got.site_settings_url.endswith(f"/w/connect/settings/connected-apps#site-{w['app'].pk}")
    assert got.site_settings_url.startswith("http")


def test_every_problem_comes_with_the_url_that_fixes_it(w):
    bare = _agents(w).agent("bare")
    assert bare.problems and len(bare.problem_links) == len(bare.problems)
    for problem, link in zip(bare.problems, bare.problem_links):
        assert link.text == problem
        assert link.url.startswith("http") and "/w/connect/agents/bare/settings#" in link.url
    reach = next(p for p in bare.problem_links if "no capability naming" in p.text)
    assert reach.url.endswith("#reach")


# --- filling a Connected site from the site's own document -----------------------------


FACTS = {"origins": ["https://labs.example.org"], "jwks_url": "https://labs.example.org/canopy/jwks.json",
         "issuer": "https://labs.example.org", "mcp_resource": "https://labs.example.org/mcp/"}


def _serve_site_json(monkeypatch, signer, *, at="https://labs.example.org/.well-known/canopy-site.json",
                     facts=FACTS, published=None, name=SITE):
    """The site serves its signed document at `at` (only) and its keys at the
    jwks_url it names (`published`, default the signer's own)."""
    calls = []

    def get(url):
        calls.append(url)
        if url != at:
            from canopy_sdk.fetch import FetchError

            raise FetchError(f"{url} answered 404", status=404)
        return site_doc.sign_site_document(signer, site_doc.site_document(name, pages=PAGES,
                                                                          site=facts))

    monkeypatch.setattr(site_needs, "_get", get)
    monkeypatch.setattr("apps.embedding.jwks._fetch",
                        lambda url: [{**public_jwk(published or signer), "use": "sig"}])
    return calls


def _discover(w, user=None, **params):
    client = Client()
    client.force_login(user or w["owner"])
    return client.get("/api/workspaces/connect/connected-apps/discover", params)


def test_a_new_site_is_found_from_its_address_alone(w, monkeypatch):
    calls = _serve_site_json(monkeypatch, generate_private_key("EdDSA"))
    response = _discover(w, address="https://labs.example.org")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["url"] == "https://labs.example.org/.well-known/canopy-site.json"
    assert calls == ["https://labs.example.org/.well-known/canopy-site.json"]
    assert body["name"] == SITE and body["site"] == FACTS
    assert body["signed_by"] == "published_keys" and body["registered_keys_verify"] is None
    assert body["current"] is None


def test_the_sdks_default_mount_is_found_too(w, monkeypatch):
    calls = _serve_site_json(monkeypatch, generate_private_key("EdDSA"),
                             at="https://labs.example.org/labs/canopy/site.json")
    body = _discover(w, address="https://labs.example.org/labs/").json()
    assert body["url"] == "https://labs.example.org/labs/canopy/site.json"
    assert calls == ["https://labs.example.org/labs/site.json",
                     "https://labs.example.org/.well-known/canopy-site.json",
                     "https://labs.example.org/labs/canopy/site.json"]


def test_a_registered_site_is_checked_against_its_own_keys_and_says_what_would_change(w, monkeypatch):
    _serve_site_json(monkeypatch, w["key"])
    before = AppCredential.objects.get(pk=w["app"].pk)
    body = _discover(w, address="https://labs.example.org", app_id=w["app"].pk).json()
    assert body["signed_by"] == "registered_keys" and body["registered_keys_verify"] is True
    assert body["current"]["origins"] == [] and body["current"]["jwks_url"] == ""
    assert body["changes"] == ["origins", "jwks_url", "issuer", "mcp_resource"]
    after = AppCredential.objects.get(pk=w["app"].pk)
    assert (after.allowed_frame_origins, after.jwks_url, after.host_issuer) == \
        (before.allowed_frame_origins, before.jwks_url, before.host_issuer), "a read never applies"


def test_adopting_the_facts_is_the_ordinary_audited_update(w, monkeypatch):
    from apps.events.models import SettingChange

    _serve_site_json(monkeypatch, w["key"])
    body = _discover(w, address="https://labs.example.org", app_id=w["app"].pk).json()
    monkeypatch.setattr("apps.host_grants.outbound.check_url", lambda url, what: url)
    monkeypatch.setattr("apps.embedding.jwks._refuse_private", lambda host: None)
    client = Client()
    client.force_login(w["owner"])
    site = body["site"]
    response = client.patch(f"/api/workspaces/connect/connected-apps/{w['app'].pk}",
                            data={"origins": site["origins"], "jwks_url": site["jwks_url"],
                                  "host_issuer": site["issuer"],
                                  "host_mcp_resource": site["mcp_resource"]},
                            content_type="application/json")
    assert response.status_code == 200, response.content
    assert response.json()["origins"] == FACTS["origins"]
    assert SettingChange.objects.filter(operation="update_connected_app").exists()


def test_a_document_signed_by_neither_the_registered_nor_the_named_keys_is_refused(w, monkeypatch):
    _serve_site_json(monkeypatch, generate_private_key("EdDSA"), published=generate_private_key("EdDSA"))
    response = _discover(w, address="https://labs.example.org", app_id=w["app"].pk)
    assert response.status_code == 422 and response.json()["detail"].startswith("bad_signature")


def test_another_sites_document_is_refused_for_this_site(w, monkeypatch):
    _serve_site_json(monkeypatch, w["key"], name="someone-else")
    response = _discover(w, address="https://labs.example.org", app_id=w["app"].pk)
    assert response.status_code == 422 and response.json()["detail"].startswith("bad_issuer")


def test_a_document_that_declares_no_facts_says_how_to_get_them(w, monkeypatch):
    _serve_site_json(monkeypatch, w["key"], facts=None)
    response = _discover(w, address="https://labs.example.org", app_id=w["app"].pk)
    assert response.status_code == 422 and response.json()["detail"].startswith("no_facts")
    assert "0.16" in response.json()["detail"]


def test_nowhere_to_find_it_lists_where_canopy_looked(w, monkeypatch):
    _serve_site_json(monkeypatch, w["key"], at="https://elsewhere.example.org/site.json")
    detail = _discover(w, address="https://labs.example.org").json()["detail"]
    assert detail.startswith("unreachable") and ".well-known/canopy-site.json" in detail


def test_only_an_owner_reads_a_site_to_fill_it(w, monkeypatch):
    _serve_site_json(monkeypatch, w["key"])
    admin = get_user_model().objects.create_user("ad", "ad@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=admin, workspace=w["ws"], role=WorkspaceMembership.ADMIN)
    stranger = get_user_model().objects.create_user("s2", "s2@dimagi.com", "pw")
    assert _discover(w, user=admin, address="https://labs.example.org").status_code == 403
    assert _discover(w, user=stranger, address="https://labs.example.org").status_code == 404


def test_candidate_urls_from_whatever_the_owner_has():
    assert site_needs.candidate_urls("https://labs.example.org/canopy/site.json") == \
        ["https://labs.example.org/canopy/site.json"]
    assert site_needs.candidate_urls("https://labs.example.org/canopy/jwks.json") == \
        ["https://labs.example.org/canopy/site.json"]
    assert site_needs.candidate_urls("labs.example.org") == \
        ["https://labs.example.org/.well-known/canopy-site.json",
         "https://labs.example.org/canopy/site.json"]
    assert site_needs.candidate_urls("") == []
