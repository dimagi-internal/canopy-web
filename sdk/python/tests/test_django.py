"""The Django integration: settings → views, stores, the gate, and the panel."""
from __future__ import annotations

import json
from io import StringIO
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.http import Http404
from django.test import Client, override_settings

from canopy_sdk import contract
from canopy_sdk.django import conf, views
from canopy_sdk.django.asgi import dpop_gate
from canopy_sdk.django.models import DelegatedToken, SeenJti
from canopy_sdk.django.stores import DjangoJtiStore
from canopy_sdk.keys import private_pem
from canopy_sdk.stores import Replayed

from .helpers import CANOPY, CLIENT_ID, ISSUER, RESOURCE, World, call_asgi, echo_app

pytestmark = pytest.mark.django_db


@pytest.fixture
def world():
    return World()


@pytest.fixture
def user():
    return get_user_model().objects.create_user("gillian", "g@example.org", "pw")


@pytest.fixture
def host(world, user):
    settings = {
        "SIGNING_KEY": private_pem(world.host_key), "CANOPY_BASE_URL": CANOPY, "APP_NAME": "connect-labs",
        "AGENT_SLUG": "ace", "CLIENT_ID": CLIENT_ID, "ISSUER": ISSUER, "RESOURCE": RESOURCE,
        "TOKEN_ENDPOINT": "http://testserver/o/token/",
        "SCOPE_TOOLS": {"marketplace:read": ["marketplace_orgs_get"]},
        "PAGE_SCOPES": {"network": ["marketplace:read"]},
    }
    cache.clear()
    with override_settings(CANOPY_HOST=settings), \
            mock.patch("canopy_sdk.host.client_keys.fetch.get_json", side_effect=world._fetch):
        yield world
    cache.clear()


def _grant(world, user, **extra):
    data = world.form(assertion=world.id_jag(sub=str(user.pk)), **extra)
    proof = world.proof(htu="http://testserver/o/token/")
    return Client().post("/o/token/", data, headers={"DPoP": proof})


def test_the_migrations_are_in_sync():
    out = StringIO()
    call_command("makemigrations", "canopy_host", "--check", "--dry-run", stdout=out)


def test_the_token_view_redeems_and_stores_only_a_hash(host, user):
    response = _grant(host, user)
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["token_type"] == "DPoP" and response["Cache-Control"] == "no-store"
    row = DelegatedToken.objects.get()
    assert row.subject == str(user.pk) and row.token_checksum == contract.token_checksum(body["access_token"])
    assert SeenJti.objects.count() == 3


def test_other_grant_types_still_reach_the_existing_token_view(host):
    response = Client().post("/o/token/", {"grant_type": "client_credentials"})
    assert response.json() == {"error": "unsupported_grant_type", "from": "toolkit"}


def test_an_inactive_user_gets_no_token(host, user):
    user.is_active = False
    user.save()
    response = _grant(host, user)
    assert response.status_code == 400 and response.json()["error"] == "invalid_grant"


def test_a_replayed_grant_is_refused_through_the_database(host, user):
    token = host.id_jag(sub=str(user.pk))
    for expected in (200, 400):
        response = Client().post("/o/token/", host.form(assertion=token),
                                 headers={"DPoP": host.proof(htu="http://testserver/o/token/")})
        assert response.status_code == expected


def test_an_unconfigured_host_refuses_the_grant(world):
    with override_settings(CANOPY_HOST={}):
        response = Client().post("/canopy/token/", world.form(), headers={"DPoP": world.proof()})
    assert response.status_code == 400 and response.json()["error"] == "unsupported_grant_type"


def test_the_jwks_view_publishes_public_keys_only(host):
    keys = Client().get("/canopy/jwks.json").json()["keys"]
    assert [k["kid"] for k in keys] == [host.config.kid] and "d" not in keys[0]
    with override_settings(CANOPY_HOST={}):
        assert Client().get("/canopy/jwks.json").status_code == 503


def test_the_django_jti_store_is_all_or_nothing():
    store = DjangoJtiStore()
    store.consume([("a", "1", 2_000_000_000)])
    with pytest.raises(Replayed):
        store.consume([("b", "2", 2_000_000_000), ("a", "1", 2_000_000_000)])
    assert not SeenJti.objects.filter(key__startswith="b:").exists()


# --- the gate ---------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_the_gate_resolves_a_token_issued_through_the_view(host, user):
    access = _grant(host, user).json()["access_token"]
    seen = []
    status, _, _ = call_asgi(dpop_gate(echo_app(seen), require_principal=True), "POST", "/mcp/",
                             headers={"Authorization": f"DPoP {access}",
                                      "DPoP": host.proof(htu=RESOURCE, access_token=access)})
    assert status == 200 and seen[0]["principal"].subject == str(user.pk)
    assert seen[0]["principal"].allowed_tools == {"marketplace_orgs_get"}


# --- the panel ---------------------------------------------------------------------------------


def _rendered_options(body: str) -> dict:
    start = body.index('<script id="canopy-panel-options" type="application/json">')
    start = body.index(">", start) + 1
    return json.loads(body[start:body.index("</script>", start)])


def _mint(client, url, *, grant=True):
    """A panel mint; `grant` is what the panel sends once the person has sent a message."""
    return client.post(url, data=json.dumps({"grant": True} if grant else {}),
                       content_type="application/json")


def _rendered_token(body: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(_rendered_options(body)["tokenUrl"]).query)
    assert "page" in query, "the registered page carries its token to the widget"
    return query["page"][0]


def test_the_rendered_token_url_is_literal(host, user):
    # `escapejs` rendered `=` as `\u003D`; json_script leaves the URL as it is.
    client = Client()
    client.force_login(user)
    body = client.get("/marketplace/network/").content.decode()
    url = _rendered_options(body)["tokenUrl"]
    assert url.startswith("/canopy/panel-token/?page=") and url in body
    assert "\\u003D" not in body and "escapejs" not in body


def test_the_csrf_fallback_is_rendered_into_an_attribute(host, user):
    import re

    client = Client()
    client.force_login(user)
    body = client.get("/marketplace/network/").content.decode()
    match = re.search(r'<script data-csrf="([^"]*)">', body)
    assert match and len(match.group(1)) >= 32


def test_a_hostile_value_cannot_end_the_script_block(host, user):
    settings = {**conf.raw(), "PANEL": {"launcher_label": "</script><script>alert(1)</script>"}}
    client = Client()
    client.force_login(user)
    with override_settings(CANOPY_HOST=settings):
        body = client.get("/marketplace/network/").content.decode()
    assert "</script><script>alert(1)" not in body
    assert _rendered_options(body)["launcherLabel"] == "</script><script>alert(1)</script>"


def test_a_registered_page_renders_a_token_that_yields_its_scopes(host, user):
    client = Client()
    client.force_login(user)
    body = client.get("/marketplace/network/").content.decode()
    assert "/embed/widget.js" in body and "canopy-page-state" in body
    assert conf.page_tokens().scopes(_rendered_token(body), user.pk) == ("marketplace:read",)


def test_an_unregistered_page_renders_the_panel_without_a_token(host, user):
    client = Client()
    client.force_login(user)
    body = client.get("/elsewhere/").content.decode()
    assert "/embed/widget.js" in body
    assert "page" not in _rendered_options(body)["tokenUrl"]


def test_nothing_renders_when_unconfigured(user):
    client = Client()
    client.force_login(user)
    assert "widget.js" not in client.get("/marketplace/network/").content.decode()


def _rendered_json(body: str, element_id: str):
    start = body.index(f'<script id="{element_id}" type="application/json">')
    start = body.index(">", start) + 1
    return json.loads(body[start:body.index("</script>", start)])


def test_a_sign_off_renders_the_frame_options_and_where_to_report(host, user):
    client = Client()
    client.force_login(user)
    body = client.get("/sign-off/").content.decode()
    options = _rendered_json(body, "canopy-sign-off-options")
    assert options["app"] == conf.raw()["APP_NAME"] and options["baseUrl"]
    # The panel's own mint, with no page: a sign-off never carries a page's scopes.
    assert options["tokenUrl"] == "/canopy/panel-token/"
    assert _rendered_json(body, "canopy-sign-off-id") == "so_abcdefghijklmnop"
    assert _rendered_json(body, "canopy-sign-off-then") == "/labs/collect/"
    assert f'{options["baseUrl"]}/embed/widget.js' in body and "window.canopy.signOff" in body


def test_a_sign_off_unconfigured_only_refuses(user):
    client = Client()
    client.force_login(user)
    body = client.get("/sign-off/").content.decode()
    assert "widget.js" not in body and "canopy is not configured here" in body


def test_the_panel_token_endpoint_turns_the_page_token_into_scopes(host, user):
    client = Client()
    client.force_login(user)
    token = _rendered_token(client.get("/marketplace/network/").content.decode())
    with mock.patch.object(views, "mint_contact_token", return_value={"token": "t", "expires_at": ""}) as mint:
        response = _mint(client, f"/canopy/panel-token/?page={token}")
    assert response.json() == {"token": "t", "expires_at": "", "kind": "contact"}
    payload = mint.call_args.args[1]
    assert payload["agent_slug"] == "ace" and payload["id_jag"]
    import jwt

    assert jwt.decode(payload["assertion"], options={"verify_signature": False})["sub"] == str(user.pk)


def test_no_grant_is_minted_until_the_panel_asks_for_one(host, user):
    # On load the panel mints with an empty body: a registered page still gets no
    # ID-JAG, so canopy holds nothing that acts as the person until they send.
    client = Client()
    client.force_login(user)
    token = _rendered_token(client.get("/marketplace/network/").content.decode())
    for body in ({}, {"grant": "yes"}, {"grant": 1}, None):
        with mock.patch.object(views, "mint_contact_token", return_value={"token": "t"}) as mint:
            if body is None:
                client.post(f"/canopy/panel-token/?page={token}", data="not json",
                            content_type="application/json")
            else:
                _mint(client, f"/canopy/panel-token/?page={token}", grant=False) if body == {} else \
                    client.post(f"/canopy/panel-token/?page={token}", data=json.dumps(body),
                                content_type="application/json")
        assert "id_jag" not in mint.call_args.args[1], body
    with mock.patch.object(views, "mint_contact_token", return_value={"token": "t"}) as mint:
        _mint(client, f"/canopy/panel-token/?page={token}")
    assert mint.call_args.args[1]["id_jag"]


def test_scopes_in_the_body_or_query_are_ignored(host, user):
    client = Client()
    client.force_login(user)
    with mock.patch.object(views, "mint_contact_token", return_value={"token": "t", "expires_at": ""}) as mint:
        client.post("/canopy/panel-token/?scope=admin&page=network",
                    data=json.dumps({"scope": "marketplace:read", "sub": "someone", "grant": True}),
                    content_type="application/json")
    assert "id_jag" not in mint.call_args.args[1]


def test_a_failed_mint_is_a_502_without_canopys_words(host, user):
    from canopy_sdk.host import MintFailed

    client = Client()
    client.force_login(user)
    with mock.patch.object(views, "mint_contact_token", side_effect=MintFailed("replayed: used")):
        response = client.post("/canopy/panel-token/")
    assert response.status_code == 502 and "replayed" not in response.content.decode()


def test_the_panel_token_endpoint_needs_a_session(host):
    assert Client().post("/canopy/panel-token/").status_code in (302, 401, 403)


def test_the_panel_passes_kind_to_the_browser_and_nothing_else(host, user):
    client = Client()
    client.force_login(user)
    vouched = {"token": "t", "expires_at": "x", "kind": "user", "host_grant": True, "contact_id": 3}
    with mock.patch.object(views, "mint_contact_token", return_value=vouched):
        assert client.post("/canopy/panel-token/").json() == {"token": "t", "expires_at": "x",
                                                                "kind": "user"}


# --- fix: the gate and siblings never 500 on configuration ------------------------------


@pytest.mark.parametrize("canopy_host", [{}, "no-grant"])
def test_the_gate_refuses_dpop_401_while_the_grant_is_off(world, canopy_host):
    if canopy_host == "no-grant":  # a signing key (assertions work) but no grant
        canopy_host = {"SIGNING_KEY": private_pem(world.host_key), "CANOPY_BASE_URL": CANOPY,
                       "APP_NAME": "connect-labs"}
    seen = []
    with override_settings(CANOPY_HOST=canopy_host):
        status, headers, body = call_asgi(dpop_gate(echo_app(seen)), "POST", "/mcp/",
                                          headers={"Authorization": "DPoP abc", "DPoP": "x.y.z"})
    assert status == 401 and not seen
    assert json.loads(body)["error"] == "invalid_dpop_proof"


def test_the_gate_passes_ordinary_traffic_through_while_the_grant_is_off(world):
    seen = []
    with override_settings(CANOPY_HOST={}):
        status, _, _ = call_asgi(dpop_gate(echo_app(seen), require_principal=True), "POST", "/mcp/",
                                 headers={"Authorization": "Bearer pat-123"})
    assert status == 200 and seen and seen[0]["principal"] is None


def test_an_unreadable_signing_key_is_a_401_not_a_500(world):
    seen = []
    bad = {"SIGNING_KEY": "not a pem", "CANOPY_BASE_URL": CANOPY, "APP_NAME": "x",
           "CLIENT_ID": CLIENT_ID, "ISSUER": ISSUER, "RESOURCE": RESOURCE,
           "TOKEN_ENDPOINT": "http://testserver/o/token/"}
    with override_settings(CANOPY_HOST=bad):
        status, _, _ = call_asgi(dpop_gate(echo_app(seen)), "POST", "/mcp/",
                                 headers={"Authorization": "DPoP abc", "DPoP": "x.y.z"})
    assert status == 401 and not seen


def test_resolve_delegated_is_none_not_an_error_while_off(world):
    from canopy_sdk.django.asgi import resolve_delegated

    with override_settings(CANOPY_HOST={}):
        assert resolve_delegated("anything", "jkt") is None
    with override_settings(CANOPY_HOST={"SIGNING_KEY": "not a pem"}):
        assert resolve_delegated("anything", None) is None


@pytest.mark.django_db(transaction=True)
def test_turning_the_grant_off_stops_tokens_issued_while_it_was_on(host, user):
    from canopy_sdk.django.asgi import resolve_delegated

    from canopy_sdk.keys import public_jwk

    access = _grant(host, user).json()["access_token"]
    jkt = public_jwk(host.client.dpop_key.public_key())["kid"]
    assert resolve_delegated(access, jkt).subject == str(user.pk)
    settings = {k: v for k, v in conf.raw().items() if k != "CLIENT_ID"}
    with override_settings(CANOPY_HOST=settings):
        assert resolve_delegated(access, jkt) is None


def test_the_metadata_views_404_while_the_grant_is_off(rf):
    for canopy_host in ({}, {"SIGNING_KEY": "not a pem"}):
        with override_settings(CANOPY_HOST=canopy_host):
            for view in (views.authorization_server_metadata_view,
                         views.protected_resource_metadata_view):
                with pytest.raises(Http404):
                    view(rf.get("/.well-known/x"))


def test_the_metadata_views_serve_while_the_grant_is_on(host, rf):
    assert json.loads(views.authorization_server_metadata_view(rf.get("/")).content)["issuer"] == ISSUER


# --- fix: CANOPY_HOST as a callable --------------------------------------------------------


def canopy_host_from_settings():
    """What a host whose values derive from later-overridden settings writes."""
    from django.conf import settings

    return {**settings.CANOPY_TEST_BASE, "AGENT_SLUG": settings.CANOPY_TEST_AGENT}


def test_canopy_host_may_be_a_callable_resolved_on_every_read(host):
    base = conf.raw()
    with override_settings(CANOPY_HOST=canopy_host_from_settings, CANOPY_TEST_BASE=base,
                           CANOPY_TEST_AGENT="ace"):
        assert conf.agent_slug() == "ace" and conf.is_configured()
        assert type(conf.raw()) is dict
        with override_settings(CANOPY_TEST_AGENT="hal"):
            assert conf.agent_slug() == "hal", "resolved per read, not frozen at import"


def test_canopy_host_may_be_a_dotted_path(host):
    with override_settings(CANOPY_HOST="tests.test_django.canopy_host_from_settings",
                           CANOPY_TEST_BASE=conf.raw(), CANOPY_TEST_AGENT="eva"):
        assert conf.agent_slug() == "eva"
        assert conf.get_host_config().kid == host.config.kid


def test_a_plain_dict_and_a_mapping_still_work(host):
    from types import MappingProxyType

    assert conf.agent_slug() == "ace"
    with override_settings(CANOPY_HOST=MappingProxyType(conf.raw())):
        assert conf.agent_slug() == "ace" and type(conf.raw()) is dict


def test_the_debug_page_never_shows_the_signing_key(host):
    from django.views.debug import SafeExceptionReporterFilter

    pem = conf.raw()["SIGNING_KEY"]
    safe = SafeExceptionReporterFilter().get_safe_settings()
    assert pem not in repr(safe["CANOPY_HOST"])
    with override_settings(CANOPY_HOST=canopy_host_from_settings, CANOPY_TEST_BASE={},
                           CANOPY_TEST_AGENT="x"):
        safe = SafeExceptionReporterFilter().get_safe_settings()
        assert pem not in repr(safe["CANOPY_HOST"])


# --- fix: the panel's token URL may name a host URL -----------------------------------------


def test_panel_token_url_name_names_a_host_url(host):
    from canopy_sdk.django.pages import panel_token_url

    assert panel_token_url() == "/canopy/panel-token/"
    with override_settings(CANOPY_HOST={**conf.raw(), "PANEL_TOKEN_URL_NAME": "elsewhere"}):
        assert panel_token_url() == "/elsewhere/"
    with override_settings(CANOPY_HOST={**conf.raw(), "PANEL_TOKEN_URL_NAME": "no-such-url"}):
        assert panel_token_url() == "/canopy/panel-token/", "an unknown name falls back"
    with override_settings(CANOPY_HOST={**conf.raw(), "PANEL_TOKEN_URL": "/literal/",
                                        "PANEL_TOKEN_URL_NAME": "elsewhere"}):
        assert panel_token_url() == "/literal/", "a literal wins"


# --- SPA mode: the browser names a page key ---------------------------------------------------


@pytest.fixture
def spa(host):
    settings = {**conf.raw(), "PAGE_MODE": "key", "PAGE_SCOPES": {"network": ["marketplace:read"]},
                "PAGE_PATTERNS": {"network": r"/marketplace/network/?"}}
    with override_settings(CANOPY_HOST=settings):
        yield host


def test_key_mode_mints_with_the_scopes_the_key_selects(spa, user):
    client = Client()
    client.force_login(user)
    for page in ("network", "/marketplace/network/"):
        with mock.patch.object(views, "mint_contact_token", return_value={"token": "t"}) as mint:
            _mint(client, f"/canopy/panel-token/?page={page}")
        payload = mint.call_args.args[1]
        import jwt

        claims = jwt.decode(payload["id_jag"], options={"verify_signature": False})
        assert claims["scope"] == "marketplace:read" and claims["sub"] == str(user.pk)


def test_key_mode_ignores_an_unknown_key_and_any_scope_sent(spa, user):
    client = Client()
    client.force_login(user)
    with mock.patch.object(views, "mint_contact_token", return_value={"token": "t"}) as mint:
        _mint(client, "/canopy/panel-token/?page=admin&scope=marketplace:read")
    assert "id_jag" not in mint.call_args.args[1]


def test_key_mode_renders_no_page_token_and_names_the_path_at_mint(spa, user):
    client = Client()
    client.force_login(user)
    body = client.get("/marketplace/network/").content.decode()
    options = _rendered_options(body)
    assert options["tokenUrl"] == "/canopy/panel-token/" and options["pageFromPath"] is True


def test_key_mode_refuses_a_write_scope_unless_listed(spa):
    tools = {"marketplace:read": ["a"], "orgs:write": ["b"]}
    with override_settings(CANOPY_HOST={**conf.raw(), "SCOPE_TOOLS": tools,
                                        "PAGE_SCOPES": {"network": ["orgs:write"]}}):
        with pytest.raises(ValueError):
            conf.page_registry()
    with override_settings(CANOPY_HOST={**conf.raw(), "SCOPE_TOOLS": tools,
                                        "PAGE_SCOPES": {"network": ["orgs:write"]},
                                        "WRITABLE_SCOPES": ["orgs:write"]}):
        assert conf.page_scopes("network", None) == ("orgs:write",)


def test_an_unknown_page_mode_is_a_configuration_error(host):
    with override_settings(CANOPY_HOST={**conf.raw(), "PAGE_MODE": "trust-me"}):
        with pytest.raises(ValueError):
            conf.page_registry()


# --- 0.5.0: a page can update what it shows without reloading ---------------------------


def test_the_panel_hands_the_page_a_handle_and_the_budget_it_trims_to(host, user):
    client = Client()
    client.force_login(user)
    body = client.get("/marketplace/network/").content.decode()
    options = _rendered_options(body)
    assert options["stateByteBudget"] == 7 * 1024 and options["maxVisibleIds"] == 400
    assert "window.canopyHost = host" in body
    assert 'new CustomEvent("canopy:ready"' in body


def _run_panel_script(body: str, calls: str) -> list:
    """Execute the rendered panel script under node against a stub widget and
    return every state it pushed. `calls` runs after the script, with `host` bound."""
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    blocks = dict(re.findall(r'<script id="([a-z-]+)" type="application/json">(.*?)</script>', body, re.S))
    script = re.search(r"<script data-csrf=\"[^\"]*\">(.*?)</script>", body, re.S).group(1)
    harness = f"""
      const pushed = [];
      const els = {json.dumps(blocks)};
      const events = [];
      const opened = [];
      let options = null;
      global.window = {{ canopy: {{ init: (o) => {{ options = o; return {{
                           setPageState: (s) => pushed.push(s),
                           openSession: (id) => opened.push(id) }}; }} }},
                         location: {{ pathname: "/" }} }};
      global.document = {{
        currentScript: null,
        getElementById: (id) => (id in els ? {{ textContent: els[id] }} : null),
        querySelector: () => null,
        dispatchEvent: (e) => {{ events.push({{ type: e.type, detail: e.detail }}); return true; }},
      }};
      global.CustomEvent = class {{ constructor(type, init) {{ this.type = type; this.detail = init.detail; }} }};
      {script}
      const host = window.canopyHost;
      {calls}
      process.stdout.write(JSON.stringify(typeof result === "undefined" ? pushed : result));
    """
    out = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_update_page_state_merges_and_keeps_what_the_server_decided(host, user):
    client = Client()
    client.force_login(user)
    body = client.get("/marketplace/network/").content.decode()
    pushed = _run_panel_script(body, """
      host.updatePageState({ visible_ids: ["llo-b"], filters: { org: "B" },
                             resource: "evil://x", backing_tool: "admin_delete" });
    """)
    first, second = pushed
    assert first["visible_ids"] == ["llo-a", "llo-b"]
    assert second["visible_ids"] == ["llo-b"] and second["filters"] == {"org": "B"}
    # A script on the page narrows the view; it cannot change what the page is
    # or the tool that reads its rows.
    assert second["resource"] == first["resource"] == "labs-marketplace://orgs"
    assert second["backing_tool"] == "marketplace_orgs_get"


def test_update_page_state_trims_a_selection_to_the_budget(host, user):
    client = Client()
    client.force_login(user)
    body = client.get("/marketplace/network/").content.decode()
    pushed = _run_panel_script(body, """
      const ids = Array.from({ length: 1000 }, (_, i) => "organisation-slug-" + i);
      host.updatePageState({ visible_ids: ids });
    """)
    last = pushed[-1]
    assert 0 < len(last["visible_ids"]) <= 400
    # Measured as canopy measures it: compact JSON, in bytes.
    assert len(json.dumps(last, separators=(",", ":")).encode()) <= 7 * 1024


def test_the_panel_relays_the_open_conversation_and_opens_one(host, user):
    """0.22.1: `canopy:session` / `canopyHost.session()` carry the widget's report
    (id, where it began, private) and nothing else; `openSession` forwards an id."""
    client = Client()
    client.force_login(user)
    body = client.get("/marketplace/network/").content.decode()
    result = _run_panel_script(body, """
      const before = host.session();
      options.onSession({ id: "0f8fad5b-d9cb-469f-a165-70867728950e", resource: "labs-data-space://7",
                          private: true, title: "never relayed" });
      const during = host.session();
      options.onSession(null);
      const forwarded = host.openSession("0f8fad5b-d9cb-469f-a165-70867728950e");
      var result = { before, during, after: host.session(), forwarded, opened,
                     sessions: events.filter((e) => e.type === "canopy:session").map((e) => e.detail) };
    """)
    report = {"id": "0f8fad5b-d9cb-469f-a165-70867728950e", "resource": "labs-data-space://7", "private": True}
    assert result["before"] is None and result["after"] is None
    assert result["during"] == report
    assert result["sessions"] == [report, None]
    assert result["forwarded"] is True and result["opened"] == ["0f8fad5b-d9cb-469f-a165-70867728950e"]


def test_runner_requirements_are_read_from_the_setting(host):
    from canopy_sdk.django import conf
    with override_settings(CANOPY_HOST={
            "SIGNING_KEY": private_pem(host.host_key), "CANOPY_BASE_URL": CANOPY,
            "APP_NAME": "connect-labs", "RUNNER_REQUIREMENTS": ["sensitive_data"]}):
        assert conf.get_host_config().runner_requirements == ("sensitive_data",)


# --- a page that chooses its own agent and runner requirements ------------------------------


def _data_host(host):
    return {**conf.raw(), "RUNNER_REQUIREMENTS": [],
            "SCOPE_TOOLS": {"marketplace:read": ["marketplace_orgs_get"], "data:read": ["explorer_query"]},
            "PAGE_SCOPES": {"network": ["marketplace:read"], "data": ["data:read"]}}


def _claims(token: str) -> dict:
    import jwt

    return jwt.decode(token, options={"verify_signature": False})


def test_a_page_that_chose_its_agent_binds_the_mint_to_it(host, user):
    client = Client()
    client.force_login(user)
    with override_settings(CANOPY_HOST=_data_host(host)):
        body = client.get("/data/visits/").content.decode()
        options = _rendered_options(body)
        with mock.patch.object(views, "mint_contact_token", return_value={"token": "t"}) as mint:
            _mint(client, f"/canopy/panel-token/?page={_rendered_token(body)}")
    assert options["agent"] == "labsdata", "the widget preselects the page's agent"
    payload = mint.call_args.args[1]
    assert payload["agent_slug"] == "labsdata"
    assertion = _claims(payload["assertion"])
    assert assertion[contract.AGENT_CLAIM] == "labsdata"
    assert assertion[contract.RUNNER_REQUIREMENTS_CLAIM] == ["sensitive_data"]
    assert _claims(payload["id_jag"])["scope"] == "data:read"


def test_the_rest_of_the_site_keeps_its_default_agent_unbound_and_without_sensitive_data(host, user):
    client = Client()
    client.force_login(user)
    with override_settings(CANOPY_HOST=_data_host(host)):
        token = _rendered_token(client.get("/marketplace/network/").content.decode())
        with mock.patch.object(views, "mint_contact_token", return_value={"token": "t"}) as mint:
            _mint(client, f"/canopy/panel-token/?page={token}")
    payload = mint.call_args.args[1]
    assertion = _claims(payload["assertion"])
    assert payload["agent_slug"] == "ace"
    assert contract.AGENT_CLAIM not in assertion
    assert contract.RUNNER_REQUIREMENTS_CLAIM not in assertion


def test_a_page_can_raise_the_sites_requirements_never_lower_them(host, user):
    client = Client()
    client.force_login(user)
    with override_settings(CANOPY_HOST={**_data_host(host), "RUNNER_REQUIREMENTS": ["sensitive_data"]}):
        token = _rendered_token(client.get("/marketplace/network/").content.decode())
        with mock.patch.object(views, "mint_contact_token", return_value={"token": "t"}) as mint:
            _mint(client, f"/canopy/panel-token/?page={token}")
    assert _claims(mint.call_args.args[1]["assertion"])[contract.RUNNER_REQUIREMENTS_CLAIM] == ["sensitive_data"]


def test_the_choice_is_inside_the_signature(host, user):
    # A browser that edits the agent out of a data page's token gets no page at
    # all — never the page's scopes for the default agent.
    import base64

    client = Client()
    client.force_login(user)
    with override_settings(CANOPY_HOST=_data_host(host)):
        token = _rendered_token(client.get("/data/visits/").content.decode())
        payload, _, sig = token.partition(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        assert claims["a"] == "labsdata" and claims["r"] == ["sensitive_data"]
        claims.pop("a")
        forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        with mock.patch.object(views, "mint_contact_token", return_value={"token": "t"}) as mint:
            _mint(client, f"/canopy/panel-token/?page={forged}.{sig}")
    sent = mint.call_args.args[1]
    assert "id_jag" not in sent and sent["agent_slug"] == "ace"
    assert contract.AGENT_CLAIM not in _claims(sent["assertion"])


def test_a_page_with_no_scopes_may_still_choose_its_agent(host, user):
    from canopy_sdk.django.pages import panel_context

    rf_request = mock.Mock(user=user, resolver_match=mock.Mock(view_name="unregistered"))
    ctx = panel_context(rf_request, agent="labsdata", runner_requirements=["sensitive_data"])
    page = conf.page_for(ctx["page_token"], user)
    assert page.scopes == () and page.agent == "labsdata" and page.runner_requirements == ("sensitive_data",)
    assert panel_context(rf_request)["page_token"] == "", "no choice and no scopes: no token"


def test_panel_options_pass_through_and_override_per_render(host, user):
    client = Client()
    client.force_login(user)
    with override_settings(CANOPY_HOST={**_data_host(host), "PANEL": {"mode": "overlay", "width": 420,
                                                                       "launcher_label": "Ask"}}):
        data = _rendered_options(client.get("/data/visits/").content.decode())
        network = _rendered_options(client.get("/marketplace/network/").content.decode())
    assert (data["mode"], data["width"], data["expandable"], data["initialDisplay"]) == \
        ("docked", 560, True, "expanded")
    assert data["launcherLabel"] == "Ask", "an override is per key, not the whole PANEL"
    assert network["mode"] == "overlay" and network["width"] == 420
    assert "expandable" not in network and "open" not in network, "unset keeps the widget's default"


def test_an_unknown_panel_key_or_bad_requirement_is_refused_where_it_is_written(host, user):
    from canopy_sdk.django.pages import panel_context

    request = mock.Mock(user=user, resolver_match=mock.Mock(view_name="data"))
    with pytest.raises(ValueError, match="unknown keys"):
        panel_context(request, panel={"heigth": 3})
    with pytest.raises(ValueError, match="unknown runner requirement"):
        panel_context(request, agent="labsdata", runner_requirements=["fast"])


def test_key_mode_reads_the_pages_agent_from_page_agents(spa, user):
    settings = {**conf.raw(), "PAGE_AGENTS": {"network": {"agent": "labsdata",
                                                          "runner_requirements": ["sensitive_data"]}}}
    client = Client()
    client.force_login(user)
    with override_settings(CANOPY_HOST=settings):
        with mock.patch.object(views, "mint_contact_token", return_value={"token": "t"}) as mint:
            _mint(client, "/canopy/panel-token/?page=/marketplace/network/")
        with pytest.raises(ValueError, match="PAGE_AGENTS"):
            from canopy_sdk.django.pages import panel_context

            panel_context(None, agent="labsdata")
    payload = mint.call_args.args[1]
    assert payload["agent_slug"] == "labsdata"
    assert _claims(payload["assertion"])[contract.AGENT_CLAIM] == "labsdata"


def test_page_agents_must_name_registered_pages(spa):
    settings = {**conf.raw(), "PAGE_AGENTS": {"nowhere": {"agent": "labsdata"}}}
    with override_settings(CANOPY_HOST=settings), pytest.raises(ValueError, match="not registered"):
        conf.page_registry()


# --- link schemes: the SDK declares them and relays updates; a host keeps its own results ---


def test_site_json_declares_the_hosts_link_schemes_and_nothing_else_derives_one(host):
    from canopy_sdk import site as site_mod

    cfg = {**conf.raw(), "RESOURCE_SCHEMES": ["labs-result", "labs-result", "labs-draft"],
           # A 0.18 setting left behind declares nothing any more.
           "ARTIFACTS": {"SCHEME": "old-result"}}
    with override_settings(CANOPY_HOST=cfg):
        response = Client().get("/canopy/site.json")
    got = site_mod.verify_site_document(response.content.decode(), [host.host_key.public_key()],
                                        site_name="connect-labs")
    assert got.site.resource_schemes == ("labs-result", "labs-draft")
    assert got.site.resource_policy() == {"labs-result": None, "labs-draft": None}


def test_site_json_declares_how_long_a_schemes_ids_are(host):
    from canopy_sdk import site as site_mod

    cfg = {**conf.raw(), "RESOURCE_SCHEMES": ["labs-result", "labs-draft"],
           "RESOURCE_ID_LENGTHS": {"labs-result": 24}}
    with override_settings(CANOPY_HOST=cfg):
        response = Client().get("/canopy/site.json")
    got = site_mod.verify_site_document(response.content.decode(), [host.host_key.public_key()],
                                        site_name="connect-labs")
    assert got.site.resource_policy() == {"labs-result": 24, "labs-draft": None}
    # A document naming a length for a scheme it does not declare, or a loose one, is malformed.
    for bad in ({"labs-other": 24}, {"labs-result": 8}, {"labs-result": "24"}, ["labs-result"]):
        with pytest.raises(site_mod.SiteDocumentError):
            site_mod.site_document("connect-labs", site={"resource_schemes": ["labs-result"],
                                                         "resource_id_lengths": bad})


def test_kept_results_are_not_the_sdks(host):
    import importlib

    from django.db import connection

    for gone in ("canopy_sdk.host.artifacts", "canopy_sdk.django.artifacts"):
        with pytest.raises(ImportError):
            importlib.import_module(gone)
    assert "canopy_host_artifact" not in connection.introspection.table_names()


def test_a_bad_resource_scheme_fails_the_hosts_system_check(host):
    from django.core import checks

    for bad in (["labs-result", "https"], ["not a scheme"]):
        with override_settings(CANOPY_HOST={**conf.raw(), "RESOURCE_SCHEMES": bad}):
            errors = [e for e in checks.run_checks() if e.id == "canopy_host.E011"]
        assert len(errors) == 1 and repr(bad[-1]) in errors[0].msg
    with override_settings(CANOPY_HOST={**conf.raw(), "RESOURCE_SCHEMES": ["labs-result"]}):
        assert not [e for e in checks.run_checks() if e.id.startswith("canopy_host.")]


def test_a_bad_resource_id_length_fails_the_hosts_system_check(host):
    from django.core import checks

    for bad in ({"labs-draft": 24}, {"labs-result": 12}, {"labs-result": "24"}, ["labs-result"]):
        cfg = {**conf.raw(), "RESOURCE_SCHEMES": ["labs-result"], "RESOURCE_ID_LENGTHS": bad}
        with override_settings(CANOPY_HOST=cfg):
            errors = [e for e in checks.run_checks() if e.id == "canopy_host.E011"]
        assert len(errors) == 1, (bad, errors)
    cfg = {**conf.raw(), "RESOURCE_SCHEMES": ["labs-result"], "RESOURCE_ID_LENGTHS": {"labs-result": 24}}
    with override_settings(CANOPY_HOST=cfg):
        assert not [e for e in checks.run_checks() if e.id.startswith("canopy_host.")]
