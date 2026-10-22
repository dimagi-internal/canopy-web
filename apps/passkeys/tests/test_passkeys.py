"""Passkeys and the step-up they earn (canopy-web#1538).

The ceremony tests drive py_webauthn's REAL verification with a software
authenticator (`apps.passkeys.testing.SoftAuthenticator`); a few failure paths
mock `verify_*_response` directly, where a crafted bad response would only be
testing the library.
"""
from __future__ import annotations

import time
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.passkeys import stepup
from apps.passkeys.models import Passkey
from apps.passkeys.testing import SoftAuthenticator, grant_step_up

pytestmark = pytest.mark.django_db


@pytest.fixture
def jj():
    return get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")


@pytest.fixture
def browser(jj):
    c = Client()
    c.force_login(jj)
    return c


def _pat(user) -> str:
    from apps.tokens.models import PersonalToken

    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return raw


def _post(client, path, body=None, **extra):
    return client.post(path, data=body or {}, content_type="application/json", **extra)


def _register(client, device: SoftAuthenticator, label="MacBook Touch ID"):
    begin = _post(client, "/api/passkeys/register/begin")
    assert begin.status_code == 200, begin.content
    return _post(client, "/api/passkeys/register/finish",
                 {"credential": device.create(begin.json()["options"]), "label": label})


def _step_up(client, device: SoftAuthenticator):
    begin = _post(client, "/api/passkeys/step-up/begin")
    assert begin.status_code == 200, begin.content
    return _post(client, "/api/passkeys/step-up/finish", {"credential": device.get(begin.json()["options"])})


def _expire_step_up(client):
    session = client.session
    session[stepup.SESSION_KEY] = {**session[stepup.SESSION_KEY], "until": time.time() - 1}
    session.save()


# --- registration ------------------------------------------------------------------

def test_first_passkey_registers_from_a_bare_session_and_opens_a_step_up(browser, jj):
    device = SoftAuthenticator()
    r = _register(browser, device)
    assert r.status_code == 201, r.content
    assert r.json()["label"] == "MacBook Touch ID"
    pk = Passkey.objects.get(user=jj)
    assert pk.credential_id == device.id and pk.transports == ["internal"]
    # The registration verified the person, so the window is open.
    assert browser.get("/api/passkeys/step-up").json()["active"] is True
    assert [p["id"] for p in browser.get("/api/passkeys/").json()] == [pk.id]


def test_registration_options_are_bound_to_the_host_and_require_verification(browser):
    opts = _post(browser, "/api/passkeys/register/begin").json()["options"]
    assert opts["rp"]["id"] == "testserver"
    assert opts["authenticatorSelection"]["userVerification"] == "required"


def test_the_passkey_list_carries_each_credential_id(browser):
    device = SoftAuthenticator()
    assert _register(browser, device).json()["credential_id"] == device.id
    assert [p["credential_id"] for p in browser.get("/api/passkeys/").json()] == [device.id]


def test_an_upgrade_leaves_passkeys_without_prf_out_of_the_exclusions(browser, jj):
    old, prf = SoftAuthenticator(), SoftAuthenticator()
    assert _register(browser, old, label="old").status_code == 201
    assert _step_up(browser, old).status_code == 200
    assert _register(browser, prf, label="prf").status_code == 201
    Passkey.objects.filter(credential_id=old.id).update(prf_enabled=None)
    Passkey.objects.filter(credential_id=prf.id).update(prf_enabled=True)

    def excluded(path):
        return {d["id"] for d in _post(browser, path).json()["options"]["excludeCredentials"]}

    assert excluded("/api/passkeys/register/begin") == {old.id, prf.id}
    # The passkey app must be allowed to make the replacement for the same account.
    assert excluded("/api/passkeys/register/begin?upgrade=true") == {prf.id}


def test_a_registration_from_another_origin_is_refused(browser):
    r = _register(browser, SoftAuthenticator(origin="https://evil.example"))
    assert r.status_code == 400
    assert not Passkey.objects.exists()


def test_a_registration_without_user_verification_is_refused(browser):
    begin = _post(browser, "/api/passkeys/register/begin").json()["options"]
    cred = SoftAuthenticator().create(begin, flags=0x41)  # UP|AT, no UV
    r = _post(browser, "/api/passkeys/register/finish", {"credential": cred})
    assert r.status_code == 400
    assert not Passkey.objects.exists()


def test_a_token_cannot_register_a_passkey(jj):
    r = _post(Client(), "/api/passkeys/register/begin", HTTP_AUTHORIZATION=f"Bearer {_pat(jj)}")
    assert r.status_code == 403
    assert r.json()["error"] == "browser_session_required"


def test_a_second_passkey_needs_a_step_up_with_the_first(browser):
    first = SoftAuthenticator()
    assert _register(browser, first).status_code == 201
    _expire_step_up(browser)

    r = _post(browser, "/api/passkeys/register/begin")
    assert r.status_code == 403 and r.json()["error"] == "step_up_required"

    assert _step_up(browser, first).status_code == 200
    assert _register(browser, SoftAuthenticator(), label="phone").status_code == 201
    assert Passkey.objects.count() == 2


def test_a_challenge_is_single_use(browser):
    device = SoftAuthenticator()
    begin = _post(browser, "/api/passkeys/register/begin").json()["options"]
    cred = device.create(begin)
    assert _post(browser, "/api/passkeys/register/finish", {"credential": cred}).status_code == 201
    replay = _post(browser, "/api/passkeys/register/finish", {"credential": cred})
    assert replay.status_code == 400
    assert "no passkey ceremony" in replay.json()["detail"]


# --- the step-up ---------------------------------------------------------------------

def test_step_up_with_no_passkey_says_register_one(browser):
    r = _post(browser, "/api/passkeys/step-up/begin")
    assert r.status_code == 409
    assert r.json()["error"] == "no_passkey"


def test_an_assertion_opens_a_five_minute_window(browser, jj):
    device = SoftAuthenticator()
    _register(browser, device)
    _expire_step_up(browser)
    assert browser.get("/api/passkeys/step-up").json()["active"] is False

    r = _step_up(browser, device)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["active"] is True and body["window_seconds"] == 300
    pk = Passkey.objects.get(user=jj)
    assert pk.last_used_at is not None and pk.sign_count == 1


def test_a_request_in_flight_during_begin_does_not_lose_the_challenge(browser):
    # A page poll loads the session before `begin` stashes its challenge and
    # saves after it (SESSION_SAVE_EVERY_REQUEST): its stale copy must not land.
    from importlib import import_module

    from django.conf import settings

    device = SoftAuthenticator()
    _register(browser, device)
    in_flight = import_module(settings.SESSION_ENGINE).SessionStore(browser.session.session_key)
    list(in_flight.items())  # read now, as a request in flight did
    begin = _post(browser, "/api/passkeys/step-up/begin")
    assert begin.status_code == 200, begin.content
    in_flight.save()

    r = _post(browser, "/api/passkeys/step-up/finish", {"credential": device.get(begin.json()["options"])})
    assert r.status_code == 200, r.content


def test_someone_elses_passkey_is_not_a_step_up_for_me(browser, jj):
    mine, theirs = SoftAuthenticator(), SoftAuthenticator()
    _register(browser, mine)
    other = get_user_model().objects.create_user(username="ot", email="ot@dimagi.com")
    oc = Client()
    oc.force_login(other)
    _register(oc, theirs)
    _expire_step_up(browser)

    begin = _post(browser, "/api/passkeys/step-up/begin").json()["options"]
    r = _post(browser, "/api/passkeys/step-up/finish", {"credential": theirs.get(begin)})
    assert r.status_code == 400
    assert browser.get("/api/passkeys/step-up").json()["active"] is False


def test_a_failed_verification_opens_no_window(browser):
    device = SoftAuthenticator()
    _register(browser, device)
    _expire_step_up(browser)
    from webauthn.helpers.exceptions import InvalidAuthenticationResponse

    with mock.patch("webauthn.verify_authentication_response",
                    side_effect=InvalidAuthenticationResponse("bad signature")):
        r = _step_up(browser, device)
    assert r.status_code == 400
    assert "bad signature" in r.json()["detail"]
    assert browser.get("/api/passkeys/step-up").json()["active"] is False


def test_a_verified_assertion_is_what_opens_the_window(browser):
    """The mocked verifier's verdict is the only thing standing between the
    request and the window — so a success from it must open one."""
    device = SoftAuthenticator()
    _register(browser, device)
    _expire_step_up(browser)
    verified = mock.Mock(new_sign_count=7)
    with mock.patch("webauthn.verify_authentication_response", return_value=verified) as v:
        assert _step_up(browser, device).status_code == 200
    assert v.call_args.kwargs["require_user_verification"] is True
    assert v.call_args.kwargs["expected_origin"] == ["http://testserver"]
    assert Passkey.objects.get().sign_count == 7


def test_the_window_expires(browser, jj):
    _register(browser, SoftAuthenticator())
    assert browser.get("/api/passkeys/step-up").json()["active"] is True
    _expire_step_up(browser)
    assert browser.get("/api/passkeys/step-up").json()["active"] is False


def test_a_step_up_belongs_to_the_user_who_earned_it(jj):
    from django.test import RequestFactory

    other = get_user_model().objects.create_user(username="ot", email="ot@dimagi.com")
    req = RequestFactory().get("/")
    req.session = {stepup.SESSION_KEY: {"user": jj.pk, "until": time.time() + 60}}
    req.user = other
    assert stepup.step_up_until(req) is None
    req.user = jj
    assert stepup.step_up_until(req) is not None


def test_a_token_can_never_step_up(jj):
    r = _post(Client(), "/api/passkeys/step-up/begin", HTTP_AUTHORIZATION=f"Bearer {_pat(jj)}")
    assert r.status_code == 403 and r.json()["error"] == "browser_session_required"


# --- deleting -------------------------------------------------------------------------

def test_deleting_a_passkey_needs_a_step_up(browser):
    _register(browser, SoftAuthenticator())
    pk = Passkey.objects.get()
    _expire_step_up(browser)
    r = browser.delete(f"/api/passkeys/{pk.id}")
    assert r.status_code == 403 and r.json()["error"] == "step_up_required"
    grant_step_up(browser)
    assert browser.delete(f"/api/passkeys/{pk.id}").status_code == 204
    assert not Passkey.objects.exists()


def test_i_cannot_delete_someone_elses_passkey(browser):
    other = get_user_model().objects.create_user(username="ot", email="ot@dimagi.com")
    oc = Client()
    oc.force_login(other)
    _register(oc, SoftAuthenticator())
    grant_step_up(browser)
    assert browser.delete(f"/api/passkeys/{Passkey.objects.get().id}").status_code == 404
    assert Passkey.objects.count() == 1


# --- the 403 shape the SPA keys off -----------------------------------------------------

def test_the_step_up_refusal_is_problem_json_with_a_top_level_error(browser):
    _register(browser, SoftAuthenticator())
    pk = Passkey.objects.get()
    _expire_step_up(browser)
    r = browser.delete(f"/api/passkeys/{pk.id}")
    assert r["Content-Type"].startswith("application/problem+json")
    body = r.json()
    assert body["error"] == "step_up_required"
    assert body["status"] == 403 and body["has_passkey"] is True
    assert body["register_url"] == "/settings#passkeys"
