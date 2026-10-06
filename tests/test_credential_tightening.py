"""A credential cannot be stretched into a stronger one, and a dead account's
tokens are dead.

Each test pairs the bypass with the legitimate path beside it, because every
one of these doors has a real caller — the Settings page, the
`canopy:canopy-web-pat-mint` loopback flow, the menubar's mint-session, and
canopy's own widget panel — and a tightening that broke one of them would be
worse than the hole.
"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory

from apps.tokens.models import (
    AppCredential,
    DelegatedToken,
    OAuthClient,
    OAuthGrant,
    PersonalToken,
)
from tests.site_tenant import host_workspace

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture()
def user():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


def _bearer(raw):
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _oauth_token(user):
    client = OAuthClient.objects.create(client_id="c-1", client_name="Claude", redirect_uris=[])
    grant = OAuthGrant.objects.create(client=client, user=user)
    raw, _ = PersonalToken.create_for_user(user=user, label="oauth", ttl=timedelta(hours=1),
                                           oauth_grant=grant)
    return raw


def _shown_app():
    admin = User.objects.create_user("site-admin", "site-admin@dimagi.com", "pw")
    app = AppCredential.create_credential(name="canopy-web", created_by=admin,
                                          workspace=host_workspace())
    app.show_on_canopy_pages = True
    app.save(update_fields=["show_on_canopy_pages"])
    return app


def _create_token(client):
    return client.post("/api/tokens/", data='{"label": "x"}', content_type="application/json")


# -- 1. a deactivated user's PAT ------------------------------------------

def test_a_deactivated_users_pat_stops_working(user):
    raw, _ = PersonalToken.create_for_user(user=user, label="cli")
    assert _bearer(raw).get("/api/me/").status_code == 200
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert PersonalToken.lookup(raw) is None
    assert _bearer(raw).get("/api/me/").status_code in (401, 403)


def test_a_deactivated_users_pat_is_refused_by_the_mcp_verifier(user):
    from asgiref.sync import async_to_sync

    from apps.mcp.auth import CanopyPATVerifier

    raw, _ = PersonalToken.create_for_user(user=user, label="cli")
    assert async_to_sync(CanopyPATVerifier().verify_token)(raw) is not None
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert async_to_sync(CanopyPATVerifier().verify_token)(raw) is None


def test_a_deactivated_users_pat_does_not_open_a_socket(user):
    from asgiref.sync import async_to_sync

    from apps.realtime.channels_auth import _user_from_bearer

    raw, _ = PersonalToken.create_for_user(user=user, label="cli")
    scope = {"headers": [(b"authorization", f"Bearer {raw}".encode())]}
    assert async_to_sync(_user_from_bearer)(scope) == user
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert async_to_sync(_user_from_bearer)(scope) is None


# -- 2. minting a credential from a narrower one --------------------------

def test_an_oauth_access_token_cannot_mint_a_pat(user):
    resp = _create_token(_bearer(_oauth_token(user)))
    assert resp.status_code == 403
    assert "oauth" in resp.json()["detail"]
    assert not PersonalToken.objects.filter(label="x").exists()


def test_a_pat_and_a_browser_session_still_mint(user):
    raw, _ = PersonalToken.create_for_user(user=user, label="cli")
    assert _create_token(_bearer(raw)).status_code == 201
    browser = Client()
    browser.force_login(user)
    assert _create_token(browser).status_code == 201


def test_an_oauth_access_token_cannot_mint_a_session(user):
    resp = _bearer(_oauth_token(user)).post("/api/debug/mint-session/")
    assert resp.status_code == 403
    raw, _ = PersonalToken.create_for_user(user=user, label="menubar")
    assert _bearer(raw).post("/api/debug/mint-session/").status_code == 200


def _cli_url():
    return "/auth/cli/authorize/?" + urlencode(
        {"cb": "http://127.0.0.1:8765/cb", "state": "s", "label": "canopy-cli"})


def test_an_oauth_access_token_cannot_use_the_cli_authorize_flow(user):
    resp = _bearer(_oauth_token(user)).post(_cli_url())
    assert resp.status_code == 403
    assert not PersonalToken.objects.filter(label="canopy-cli").exists()


def test_the_cli_authorize_flow_still_mints_from_a_browser(user):
    browser = Client()
    browser.force_login(user)
    resp = browser.post(_cli_url())
    assert resp.status_code == 302
    assert resp["Location"].startswith("http://127.0.0.1:8765/cb?token=")


def test_the_refusal_reads_the_credential_that_authenticated():
    from apps.tokens.middleware import minting_refusal

    request = RequestFactory().get("/")
    assert minting_refusal(request) is None
    for kind in ("oauth", "delegated", "contact"):
        request.auth_credential = {"type": kind}
        assert minting_refusal(request)
    request.auth_credential = {"type": "pat"}
    assert minting_refusal(request) is None


# -- 3. the panel's token endpoint is a browser session's only -------------

def test_a_delegated_token_cannot_mint_the_panels_token(user):
    """`/api/embed/` is inside the delegated surface, so without the check a
    site's token renews itself as canopy's own app, forever."""
    app = _shown_app()
    raw, _ = DelegatedToken.issue(app=app, user=user, ttl_seconds=600)
    before = DelegatedToken.objects.count()
    resp = _bearer(raw).post("/api/embed/token", data={}, content_type="application/json")
    assert resp.status_code == 403
    assert DelegatedToken.objects.count() == before


def test_a_pat_cannot_mint_the_panels_token_either(user):
    _shown_app()
    raw, _ = PersonalToken.create_for_user(user=user, label="cli")
    resp = _bearer(raw).post("/api/embed/token", data={}, content_type="application/json")
    assert resp.status_code == 403


def test_the_panel_still_mints_with_its_session(user):
    _shown_app()
    browser = Client()
    browser.force_login(user)
    resp = browser.post("/api/embed/token", data={}, content_type="application/json")
    assert resp.status_code == 200
    assert resp.json()["token"]


# -- 6. ?next= cannot leave canopy -----------------------------------------

@pytest.mark.parametrize("url, safe", [
    ("/w/x", True),
    ("/w/x?tab=1", True),
    ("https://canopy.dimagi.com/w/x", True),
    ("https://labs.connect.dimagi.com/canopy/supervisor", True),
    ("https://evil.example/", False),
    ("//evil.example/", False),
    ("https:evil.example", False),
    ("javascript:alert(1)", False),
])
def test_next_must_stay_on_canopy(settings, url, safe):
    from allauth.core import context

    from apps.common.auth_adapter import CustomAccountAdapter

    settings.ALLOWED_HOSTS = ["*"]  # labs' setting, which allauth's own check trusted
    settings.CANOPY_PUBLIC_BASE_URL = "https://canopy.dimagi.com"
    settings.CANOPY_IDENTITY_BASE_URL = "https://canopy.dimagi.com"
    settings.CANOPY_FORMER_BASE_URLS = ["https://labs.connect.dimagi.com/canopy"]
    request = RequestFactory().get("/accounts/google/login/", HTTP_HOST="canopy.dimagi.com")
    with context.request_context(request):
        assert CustomAccountAdapter(request).is_safe_url(url) is safe
