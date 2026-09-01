"""Tests for apps.tokens.cli_authorize_views — gh-style loopback PAT mint flow."""
from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import AsyncRequestFactory, override_settings
from django.urls import reverse

from apps.tokens.cli_authorize_views import _validate_callback, cli_authorize
from apps.tokens.models import PersonalToken

User = get_user_model()


# ---------------------------------------------------------------------------
# _validate_callback — pure-function unit tests
# ---------------------------------------------------------------------------


def test_validate_callback_accepts_loopback():
    assert _validate_callback("http://127.0.0.1:54321/cb") == "http://127.0.0.1:54321/cb"
    assert _validate_callback("http://localhost:8080/cb") == "http://localhost:8080/cb"


def test_validate_callback_rejects_non_loopback():
    assert _validate_callback("http://evil.com:80/cb") is None
    assert _validate_callback("http://10.0.0.1:54321/cb") is None
    assert _validate_callback("http://example.org:8080/cb") is None


def test_validate_callback_rejects_https():
    assert _validate_callback("https://127.0.0.1:54321/cb") is None


def test_validate_callback_rejects_userinfo():
    assert _validate_callback("http://user:pass@127.0.0.1:54321/cb") is None


def test_validate_callback_rejects_no_port_or_privileged_port():
    assert _validate_callback("http://127.0.0.1/cb") is None
    assert _validate_callback("http://127.0.0.1:80/cb") is None
    assert _validate_callback("http://127.0.0.1:1023/cb") is None


def test_validate_callback_rejects_empty_or_garbage():
    assert _validate_callback("") is None
    assert _validate_callback("not-a-url") is None
    assert _validate_callback("javascript:alert(1)") is None


# ---------------------------------------------------------------------------
# /auth/cli/authorize/ view — integration tests
# ---------------------------------------------------------------------------


def _qs(**overrides) -> dict:
    base = {
        "cb": "http://127.0.0.1:54321/cb",
        "state": "nonce-abc",
        "label": "test-label",
    }
    base.update(overrides)
    return base


@pytest.fixture
def authed_client(db, client):
    user = User.objects.create_user(
        username="alice", email="alice@dimagi.com", password="pw"
    )
    client.force_login(user)
    return client, user


@override_settings(REQUIRE_AUTH=False)  # bypass LoginRequiredMiddleware
@pytest.mark.django_db
def test_get_renders_authorize_page(authed_client):
    client, _ = authed_client
    resp = client.get(reverse("cli_authorize") + "?" + urlencode(_qs()))
    assert resp.status_code == 200
    assert b"Authorize CLI access" in resp.content
    assert b"test-label" in resp.content
    assert b"127.0.0.1:54321" in resp.content
    assert b"alice@dimagi.com" in resp.content


@override_settings(REQUIRE_AUTH=False)
@pytest.mark.django_db
def test_post_mints_token_and_redirects(authed_client):
    client, user = authed_client
    resp = client.post(reverse("cli_authorize") + "?" + urlencode(_qs()))
    assert resp.status_code == 302

    parsed = urlparse(resp["Location"])
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port == 54321
    assert parsed.path == "/cb"

    qs = parse_qs(parsed.query)
    assert qs["state"] == ["nonce-abc"]
    assert "token" in qs
    raw = qs["token"][0]

    # The raw token must round-trip through PersonalToken.lookup.
    token = PersonalToken.lookup(raw)
    assert token is not None
    assert token.user_id == user.pk
    assert token.label == "test-label"


@override_settings(REQUIRE_AUTH=False)
@pytest.mark.django_db
def test_invalid_cb_rejected(authed_client):
    client, _ = authed_client
    resp = client.get(reverse("cli_authorize") + "?" + urlencode(_qs(cb="https://evil.com:443/cb")))
    assert resp.status_code == 400
    assert b"invalid or missing cb" in resp.content


@override_settings(REQUIRE_AUTH=False)
@pytest.mark.django_db
def test_missing_state_rejected(authed_client):
    client, _ = authed_client
    resp = client.get(reverse("cli_authorize") + "?" + urlencode(_qs(state="")))
    assert resp.status_code == 400
    assert b"missing state" in resp.content


@override_settings(REQUIRE_AUTH=False)
@pytest.mark.django_db
def test_label_truncated_to_max_length(authed_client):
    client, user = authed_client
    long_label = "x" * 200
    resp = client.post(reverse("cli_authorize") + "?" + urlencode(_qs(label=long_label)))
    assert resp.status_code == 302
    token = PersonalToken.objects.filter(user=user).first()
    assert token is not None
    assert len(token.label) <= 64


@override_settings(REQUIRE_AUTH=False)
@pytest.mark.django_db
def test_label_defaults_when_blank(authed_client):
    client, user = authed_client
    resp = client.post(reverse("cli_authorize") + "?" + urlencode(_qs(label="")))
    assert resp.status_code == 302
    token = PersonalToken.objects.filter(user=user).first()
    assert token.label == "canopy-cli"


@override_settings(REQUIRE_AUTH=False)
@pytest.mark.django_db
def test_unauthenticated_get_redirects_to_login(db, client):
    """Without a session, @login_required bounces to LOGIN_URL with ?next=."""
    resp = client.get(reverse("cli_authorize") + "?" + urlencode(_qs()))
    assert resp.status_code == 302
    # Django's @login_required preserves the full request path (incl. query) in ?next=
    assert "/accounts/google/login/" in resp["Location"]
    assert "next=" in resp["Location"]


@override_settings(REQUIRE_AUTH=False)
@pytest.mark.django_db
def test_method_not_allowed(authed_client):
    client, _ = authed_client
    resp = client.delete(reverse("cli_authorize") + "?" + urlencode(_qs()))
    assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Script-prefix regression — the deployed instance runs at /canopy
# ---------------------------------------------------------------------------
#
# These use AsyncRequestFactory, NOT the default test client, and that choice is
# the whole point. The bug is ASGI-only: WSGIRequest rebuilds `path` as
# SCRIPT_NAME + PATH_INFO, so under the ordinary (WSGI) test client
# get_full_path() ALREADY carries /canopy and the broken code passes. ASGIRequest
# instead sets `self.path = scope["path"]` verbatim, and canopy-web's ASGI
# wrapper has already stripped the prefix from that scope — which is exactly
# what production does. Written against the WSGI client, every assertion below
# passes on the unfixed view (measured, 2026-09-01).


def _asgi_get(path="/auth/cli/authorize/", **overrides):
    """A request shaped like the one uvicorn hands Django behind /canopy."""
    request = AsyncRequestFactory().get(path, _qs(**overrides))
    request.user = User(username="alice", email="alice@dimagi.com")
    return request


@override_settings(FORCE_SCRIPT_NAME="/canopy")
def test_form_action_carries_the_script_prefix():
    """The Authorize button must POST back to canopy-web, not its host tenant.

    request.get_full_path() names a Connect Labs URL here; posting there 404s
    with Resolver404, which reads like a canopy-web outage rather than a
    canopy-web bug. See apps.common.script_prefix.
    """
    resp = cli_authorize(_asgi_get())
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'action="/canopy/auth/cli/authorize/?' in body
    # the handshake params must survive into the POST target
    assert "state=nonce-abc" in body


def test_form_action_has_no_prefix_when_unprefixed():
    # local dev and GCP: FORCE_SCRIPT_NAME unset, nothing to re-add
    resp = cli_authorize(_asgi_get())
    assert resp.status_code == 200
    assert 'action="/auth/cli/authorize/?' in resp.content.decode()
