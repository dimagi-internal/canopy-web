"""Signing an MCP client in with OAuth (`apps/tokens/mcp_oauth.py`), end to end.

Walked the way an MCP client walks it — discovery, registration, the consent
page, the code exchange, refresh — through the real URLs and middleware, and
then the minted token is used against REST and the MCP verifier, because a
login that yields a token nothing accepts is not a login.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings

from apps.mcp.auth import CanopyPATVerifier
from apps.tokens.models import OAuthGrant, PersonalToken

User = get_user_model()
pytestmark = pytest.mark.django_db

REDIRECT = "http://127.0.0.1:33418/callback"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()


@pytest.fixture()
def user():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def browser(user):
    c = Client()
    c.force_login(user)
    return c


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _register(redirect_uris=(REDIRECT,), name="Claude Code"):
    return Client().post("/oauth/register", {"client_name": name, "redirect_uris": list(redirect_uris)},
                         content_type="application/json")


def _client_id():
    resp = _register()
    assert resp.status_code == 201, resp.content
    return resp.json()["client_id"]


def _authorize_params(client_id, challenge, **over):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-1",
              "scope": "canopy"}
    params.update(over)
    return params


def _approve(browser, client_id, challenge, **over):
    resp = browser.post("/oauth/authorize", {**_authorize_params(client_id, challenge, **over),
                                             "decision": "approve"})
    assert resp.status_code == 302, resp.content
    return parse_qs(urlsplit(resp["Location"]).query), resp["Location"]


def _exchange(client_id, code, verifier, **over):
    form = {"grant_type": "authorization_code", "client_id": client_id, "code": code,
            "code_verifier": verifier, "redirect_uri": REDIRECT}
    form.update(over)
    return Client().post("/oauth/token", form)


def _login(browser):
    client_id = _client_id()
    verifier, challenge = _pkce()
    query, _ = _approve(browser, client_id, challenge)
    resp = _exchange(client_id, query["code"][0], verifier)
    assert resp.status_code == 200, resp.content
    return client_id, resp.json()


def _works_on_rest(access_token) -> bool:
    return Client().get("/api/me/", HTTP_AUTHORIZATION=f"Bearer {access_token}").status_code == 200


# -- discovery -------------------------------------------------------------

def test_the_mcp_names_its_authorization_server():
    doc = Client().get("/.well-known/oauth-protected-resource/api/mcp/").json()
    assert doc["resource"].endswith("/api/mcp/")
    assert doc["authorization_servers"]
    assert "canopy" in doc["scopes_supported"]


def test_the_authorization_server_offers_a_browser_login():
    resp = Client().get("/.well-known/oauth-authorization-server")
    doc = resp.json()
    assert doc["authorization_endpoint"].endswith("/oauth/authorize")
    assert doc["registration_endpoint"].endswith("/oauth/register")
    assert {"authorization_code", "refresh_token"} <= set(doc["grant_types_supported"])
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert "none" in doc["token_endpoint_auth_methods_supported"]
    assert resp["Access-Control-Allow-Origin"] == "*"


def test_the_host_grant_is_still_advertised_beside_it(settings):
    """canopy's own jwt-bearer grant (embedded pages) shares the issuer."""
    from apps.tokens import self_host

    if not self_host.configured():
        pytest.skip("host keys are not configured in this settings module")
    doc = Client().get("/.well-known/oauth-authorization-server").json()
    assert "urn:ietf:params:oauth:grant-type:jwt-bearer" in doc["grant_types_supported"]
    assert "private_key_jwt" in doc["token_endpoint_auth_methods_supported"]


@override_settings(CANOPY_PUBLIC_BASE_URL="https://labs.example.com/canopy")
def test_a_401_from_the_mcp_points_at_the_metadata():
    from apps.mcp.server import _verifier

    url = str(_verifier()._get_resource_url("/"))
    assert url == "https://labs.example.com/canopy/api/mcp/"


# -- registration ----------------------------------------------------------

@pytest.mark.parametrize("uri", [
    "http://127.0.0.1:5555/cb", "http://localhost/cb", "https://claude.ai/api/mcp/auth_callback",
    "cursor://anysphere.cursor-retrieval/oauth/callback",
])
def test_registration_accepts_real_client_redirects(uri):
    assert _register([uri]).status_code == 201


@pytest.mark.parametrize("uri", [
    "http://evil.example.com/cb", "javascript:alert(1)", "data:text/html,x", "file:///etc/passwd",
    "https://ok.example.com/cb#frag", "",
])
def test_registration_refuses_redirects_that_could_leak_a_code(uri):
    resp = _register([uri])
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"


def test_registration_is_rate_limited(settings):
    from apps.tokens import views_mcp_oauth

    for _ in range(views_mcp_oauth.REGISTER_LIMIT):
        assert _register().status_code == 201
    assert _register().status_code == 429



def test_rewriting_the_first_forwarded_entry_does_not_dodge_the_limit(db):
    """The ALB appends the real address; a client rotating the entry it writes
    itself still lands in one bucket (apps/common/client_ip.py)."""
    from apps.tokens import views_mcp_oauth

    def register(i):
        return Client().post(
            "/oauth/register", {"client_name": "x", "redirect_uris": [REDIRECT]},
            content_type="application/json",
            HTTP_X_FORWARDED_FOR=f"198.51.100.{i}, 203.0.113.7",
        )

    for i in range(views_mcp_oauth.REGISTER_LIMIT):
        assert register(i).status_code == 201
    assert register(999).status_code == 429

# -- the consent page ------------------------------------------------------

def test_an_anonymous_visitor_is_sent_to_sign_in_and_back():
    client_id = _client_id()
    _, challenge = _pkce()
    resp = Client().get("/oauth/authorize", _authorize_params(client_id, challenge))
    assert resp.status_code == 302
    assert "login" in resp["Location"] and "oauth%2Fauthorize" in resp["Location"]


def test_the_consent_page_names_the_client_and_the_person(browser):
    client_id = _client_id()
    _, challenge = _pkce()
    resp = browser.get("/oauth/authorize", _authorize_params(client_id, challenge))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Claude Code" in body and "jj@dimagi.com" in body and "127.0.0.1:33418" in body


def test_an_unknown_client_is_shown_an_error_not_redirected(browser):
    _, challenge = _pkce()
    resp = browser.get("/oauth/authorize", _authorize_params("mcp_nope", challenge))
    assert resp.status_code == 400
    assert "Location" not in resp


def test_an_unregistered_redirect_is_never_followed(browser):
    """Redirecting an error to an unverified URI is an open redirect."""
    client_id = _client_id()
    _, challenge = _pkce()
    resp = browser.get("/oauth/authorize",
                       _authorize_params(client_id, challenge, redirect_uri="https://evil.example.com/cb"))
    assert resp.status_code == 400
    assert "Location" not in resp


def test_a_loopback_redirect_may_use_another_port(browser):
    client_id = _client_id()
    _, challenge = _pkce()
    query, location = _approve(browser, client_id, challenge,
                               redirect_uri="http://127.0.0.1:40001/callback")
    assert location.startswith("http://127.0.0.1:40001/callback?")
    assert "code" in query


def test_pkce_is_required(browser):
    client_id = _client_id()
    resp = browser.get("/oauth/authorize", _authorize_params(client_id, "", code_challenge_method=""))
    assert resp.status_code == 302
    query = parse_qs(urlsplit(resp["Location"]).query)
    assert query["error"] == ["invalid_request"] and query["state"] == ["st-1"]


def test_a_token_for_another_resource_is_refused(browser):
    client_id = _client_id()
    _, challenge = _pkce()
    resp = browser.get("/oauth/authorize",
                       _authorize_params(client_id, challenge, resource="https://other.example.com/mcp"))
    assert parse_qs(urlsplit(resp["Location"]).query)["error"] == ["invalid_target"]


def test_denying_sends_the_client_access_denied(browser):
    client_id = _client_id()
    _, challenge = _pkce()
    resp = browser.post("/oauth/authorize", {**_authorize_params(client_id, challenge),
                                             "decision": "deny"})
    query = parse_qs(urlsplit(resp["Location"]).query)
    assert query["error"] == ["access_denied"] and "code" not in query


def test_a_session_minted_from_a_token_cannot_approve(user):
    from apps.common.views_debug import DEBUG_SESSION_MARKER

    c = Client()
    c.force_login(user)
    session = c.session
    session[DEBUG_SESSION_MARKER] = True
    session.save()
    client_id = _client_id()
    _, challenge = _pkce()
    resp = c.post("/oauth/authorize", {**_authorize_params(client_id, challenge), "decision": "approve"})
    assert resp.status_code == 403


# -- tokens ----------------------------------------------------------------

def test_the_login_yields_a_token_that_rest_and_the_mcp_accept(browser, user):
    _, tokens = _login(browser)
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600
    assert tokens["refresh_token"]
    assert _works_on_rest(tokens["access_token"])
    access = async_to_sync(CanopyPATVerifier().verify_token)(tokens["access_token"])
    assert access is not None and access.claims["user_id"] == user.pk


def test_a_wrong_verifier_is_refused(browser):
    client_id = _client_id()
    _, challenge = _pkce()
    query, _ = _approve(browser, client_id, challenge)
    resp = _exchange(client_id, query["code"][0], secrets.token_urlsafe(48))
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_grant"


def test_a_code_is_single_use_and_a_replay_revokes_what_it_bought(browser):
    client_id = _client_id()
    verifier, challenge = _pkce()
    query, _ = _approve(browser, client_id, challenge)
    first = _exchange(client_id, query["code"][0], verifier).json()
    assert _works_on_rest(first["access_token"])
    replay = _exchange(client_id, query["code"][0], verifier)
    assert replay.status_code == 400
    assert not _works_on_rest(first["access_token"])


def test_a_code_is_bound_to_its_client(browser):
    client_id = _client_id()
    other = _client_id()
    verifier, challenge = _pkce()
    query, _ = _approve(browser, client_id, challenge)
    assert _exchange(other, query["code"][0], verifier).json()["error"] == "invalid_grant"


def test_refresh_rotates_and_retires_the_old_access_token(browser):
    client_id, tokens = _login(browser)
    resp = Client().post("/oauth/token", {"grant_type": "refresh_token", "client_id": client_id,
                                          "refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 200, resp.content
    fresh = resp.json()
    assert fresh["refresh_token"] != tokens["refresh_token"]
    assert _works_on_rest(fresh["access_token"])
    assert not _works_on_rest(tokens["access_token"])


def test_a_reused_refresh_token_revokes_the_connection(browser):
    client_id, tokens = _login(browser)
    fresh = Client().post("/oauth/token", {"grant_type": "refresh_token", "client_id": client_id,
                                           "refresh_token": tokens["refresh_token"]}).json()
    replay = Client().post("/oauth/token", {"grant_type": "refresh_token", "client_id": client_id,
                                            "refresh_token": tokens["refresh_token"]})
    assert replay.status_code == 400
    assert not _works_on_rest(fresh["access_token"])
    assert OAuthGrant.objects.get().revoked_at is not None


def test_an_unknown_grant_type_is_refused():
    resp = Client().post("/oauth/token", {"grant_type": "password"})
    assert resp.status_code == 400 and resp.json()["error"] == "unsupported_grant_type"


# -- connected apps --------------------------------------------------------

def test_connected_apps_are_listed_apart_from_hand_minted_tokens(browser, user):
    _login(browser)
    PersonalToken.create_for_user(user=user, label="by hand")
    apps = browser.get("/api/tokens/connected-apps").json()
    assert [a["client_name"] for a in apps["apps"]] == ["Claude Code"]
    assert apps["mcp_url"].endswith("/api/mcp/")
    assert [t["label"] for t in browser.get("/api/tokens/").json()] == ["by hand"]


def test_disconnecting_stops_the_token_and_the_refresh(browser):
    client_id, tokens = _login(browser)
    grant_id = browser.get("/api/tokens/connected-apps").json()["apps"][0]["id"]
    assert browser.delete(f"/api/tokens/connected-apps/{grant_id}").status_code == 204
    assert not _works_on_rest(tokens["access_token"])
    resp = Client().post("/oauth/token", {"grant_type": "refresh_token", "client_id": client_id,
                                          "refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 400


def test_nobody_else_can_disconnect_your_app(browser):
    _login(browser)
    grant_id = OAuthGrant.objects.get().pk
    other = Client()
    other.force_login(User.objects.create_user("sam", "sam@dimagi.com", "pw"))
    assert other.delete(f"/api/tokens/connected-apps/{grant_id}").status_code == 404
    assert OAuthGrant.objects.get().revoked_at is None
