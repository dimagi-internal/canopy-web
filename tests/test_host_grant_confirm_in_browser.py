"""Confirming a page's act-as-you permission in the person's BROWSER, for a panel that
cannot use a passkey itself (the Canopy Runner desktop app's web view has none).

* `/api/me/host-grants/<id>/confirm/begin|finish` confirm the same waiting grant the
  panel would, with a real WebAuthn assertion (`SoftAuthenticator`) on the person's own
  signed-in browser session, and the panel's own read then sees it confirmed.
* The id alone confirms nothing: another person's session gets a 404, and a session
  minted from a token (how the desktop app signs in) is refused outright.
* `/api/me/` says which kind of session it is (`passkeys_here`), so the app knows to
  hand a passkey to the browser instead of asking for one it cannot get.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.common.views_debug import DEBUG_SESSION_MARKER
from apps.tokens.models import DelegatedToken
from tests.fixtures import passkey_confirm
from tests.fixtures import site_gateway as sg

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    return sg.world()


def _browser(user, *, minted: bool = False) -> Client:
    client = Client()
    client.force_login(user)
    if minted:
        session = client.session
        session[DEBUG_SESSION_MARKER] = True
        session.save()
    return client


def _begin(client: Client, grant_id: int):
    return client.post(f"/api/me/host-grants/{grant_id}/confirm/begin", content_type="application/json")


def _finish(client: Client, grant_id: int, credential: dict):
    return client.post(f"/api/me/host-grants/{grant_id}/confirm/finish",
                       data=json.dumps({"credential": credential}), content_type="application/json")


def test_the_browser_confirms_the_panels_waiting_grant_and_the_panel_sees_it(w):
    grant = sg.grant(w, confirmed_at=None)
    auth = passkey_confirm.give_passkey(w["visitor"])
    browser = _browser(w["visitor"])

    shown = browser.get(f"/api/me/host-grants/{grant.pk}")
    assert shown.status_code == 200, shown.content
    assert shown.json()["confirmed"] is False and shown.json()["site"] == w["app"].name

    started = _begin(browser, grant.pk)
    assert started.status_code == 200, started.content
    done = _finish(browser, grant.pk, auth.get(started.json()["options"]))
    assert done.status_code == 200, done.content
    assert done.json()["confirmed"] is True

    raw, _ = DelegatedToken.issue(app=w["app"], user=w["visitor"], ttl_seconds=600, host_grant=grant)
    panel = passkey_confirm.panel(raw).get("/api/embed/host-grant")
    assert panel.json()["grant"]["confirmed"] is True


def test_someone_elses_session_cannot_see_or_confirm_it(w):
    grant = sg.grant(w, confirmed_at=None)
    other = User.objects.create_user("other", "other@example.com")
    passkey_confirm.give_passkey(other)
    browser = _browser(other)
    assert browser.get(f"/api/me/host-grants/{grant.pk}").status_code == 404
    assert _begin(browser, grant.pk).status_code == 404
    grant.refresh_from_db()
    assert grant.confirmed_at is None


def test_a_token_minted_session_is_refused(w):
    grant = sg.grant(w, confirmed_at=None)
    passkey_confirm.give_passkey(w["visitor"])
    refused = _begin(_browser(w["visitor"], minted=True), grant.pk)
    assert refused.status_code == 403, refused.content
    assert refused.json()["error"] == "browser_session_required"


def test_finishing_without_a_ceremony_is_refused(w):
    grant = sg.grant(w, confirmed_at=None)
    auth = passkey_confirm.give_passkey(w["visitor"])
    browser = _browser(w["visitor"])
    # A challenge this session never began: py_webauthn's check fails.
    fake = auth.get({"challenge": "AAAA", "rpId": "testserver", "allowCredentials": []})
    assert _finish(browser, grant.pk, fake).status_code == 400
    grant.refresh_from_db()
    assert grant.confirmed_at is None


def test_me_says_whether_a_passkey_works_on_this_session(w):
    assert _browser(w["visitor"]).get("/api/me/").json()["passkeys_here"] is True
    assert _browser(w["visitor"], minted=True).get("/api/me/").json()["passkeys_here"] is False
