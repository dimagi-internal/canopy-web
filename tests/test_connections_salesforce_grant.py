"""Allowing Salesforce for screens for a while: a grant the person times, that canopy-web renews and
then lets go of.

Pinned: the start of a grant lands on the page that asks how long, and only its POST starts one; the
length is checked (`temporary.LONGEST_GRANT`); the grant keeps the refresh token encrypted, only until
its end; a screen's call runs on it first, renewing an access token that ran out (or was refused), and
otherwise on the stored connection canopy-web may use; with neither the refusal carries the grant's
start; the sweep and ending it revoke the refresh token at Salesforce. Salesforce is stubbed at the
`requests` boundary, as the other suites do: `FakeSF` for its token and revoke endpoints, `Salesforce`
for its API.
"""
from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.common.encryption import decrypt_secret, encrypt_secret
from apps.connections import call_service, keeper, server_key, server_tokens, temporary
from apps.connections.models import Connection, TemporaryAccess
from apps.dynamic_views import services as view_services
from apps.events.models import Event
from tests.fixtures.connection_calls import Answer, Salesforce, TokensWorld
from tests.fixtures.connections import (
    REVOKE_URL,
    FakeSF,
    _resp,
    _token_body,
    sealed_to_owner,
)

pytestmark = pytest.mark.django_db

START = "/auth/connections/salesforce/w/start/"
CALLBACK = "/auth/connections/salesforce/w/callback/"
CALL = "/api/connections/salesforce/request"
INSTANCE = "https://dimagi.my.salesforce.com"
CODE = "function View({ canopy }) { return <div/>; }"
DECL = {"provider": "salesforce", "as": "viewer", "reads": [{"method": "GET", "path": "query"}],
        "writes": [{"method": "PATCH", "path": "sobjects/Lead/*"}]}
LEAD = "00Q5g00000AbCdEFGH"


@pytest.fixture(autouse=True)
def _clean():
    server_tokens._forget_all()
    cache.clear()
    yield
    server_tokens._forget_all()
    cache.clear()


@pytest.fixture
def key():
    k = ec.generate_private_key(ec.SECP256R1())
    server_key.use_software_key_for_tests(k)
    yield k
    server_key.use_software_key_for_tests(None)


@pytest.fixture
def w(key):
    """jj, a member of `w` whose Salesforce app is set up, with a view that reads their own Salesforce,
    and nothing connected or allowed yet."""
    world = TokensWorld()
    world.view = view_services.create(workspace_id="w", project=None, user=world.person,
                                      data={"title": "Leads", "code": CODE, "connections": [DECL]})
    world.client = Client()
    world.client.force_login(world.person)
    return world


def _allow(client, minutes="1440", **params):
    return client.post(START, {"temporary": "1", "for_screens": "1", "minutes": minutes,
                               "return_to": "/w/w/apps/sales/motions", **params})


def _granted(world, fake, minutes="1440"):
    state = parse_qs(urlparse(_allow(world.client, minutes)["Location"]).query)["state"][0]
    return world.client.get(CALLBACK, {"code": "c", "state": state})


def _grant(world, *, ends_in=timedelta(days=1), access_in=timedelta(minutes=30), token="00D!grant_access",
           refresh="5Aep_grant_refresh"):
    return TemporaryAccess.objects.create(
        app=world.app, user=world.person, agent=None, provider="salesforce", account_id="00Dorg/005user",
        account_label="jj@example.org", scopes="api refresh_token", token=encrypt_secret(token),
        refresh_token=encrypt_secret(refresh), instance_url=INSTANCE,
        access_expires_at=timezone.now() + access_in, expires_at=timezone.now() + ends_in)


def _call(world):
    body = {"method": "GET", "path": "query", "query": {"q": "SELECT Id FROM Lead"},
            "surface": {"kind": "view", "id": world.view.pk, "version": world.view.version}}
    return world.client.post(CALL, json.dumps(body), content_type="application/json")


def _bearer(call) -> str:
    return call["headers"]["Authorization"].removeprefix("Bearer ")


# ---- choosing how long, and the sign-in ---------------------------------------------------------

def test_a_link_lands_on_the_page_that_asks_how_long(w):
    resp = w.client.get(START, {"temporary": "1", "for_screens": "1", "minutes": "43200",
                                "return_to": "/w/w/apps/sales/motions"})
    assert resp.status_code == 302
    assert resp["Location"] == "/w/w/connections/salesforce/allow?return_to=%2Fw%2Fw%2Fapps%2Fsales%2Fmotions"


def test_the_pages_post_signs_in_asking_for_a_refresh_token(w):
    resp = _allow(w.client, "480")
    query = parse_qs(urlparse(resp["Location"]).query)
    assert resp["Location"].startswith("https://login.salesforce.com/services/oauth2/authorize?")
    assert "refresh_token" in query["scope"][0].split()


@pytest.mark.parametrize("minutes", ["4", "43201", "a day", "-60"])
def test_a_length_outside_the_limits_is_refused_before_salesforce(w, minutes):
    resp = _allow(w.client, minutes)
    assert "reason=bad_duration" in resp["Location"] and "salesforce.com" not in resp["Location"]


def test_the_longest_is_thirty_days(w):
    assert temporary.LONGEST_GRANT == timedelta(days=30)
    assert temporary.grant_length("salesforce", str(30 * 24 * 60)) == temporary.LONGEST_GRANT
    assert temporary.grant_length("salesforce", "") == temporary.DEFAULT_GRANT == timedelta(hours=1)
    # Google is never held longer than its one token.
    assert temporary.grant_length("google", "120") is None


def test_the_grant_keeps_the_refresh_token_encrypted_until_the_end_chosen(w):
    with FakeSF() as fake:
        resp = _granted(w, fake, minutes=str(7 * 24 * 60))
    assert resp.status_code == 302 and resp["Location"].startswith("/w/w/apps/sales/motions?")
    assert "status=ok" in resp["Location"]
    row = TemporaryAccess.objects.get()
    assert row.agent is None and row.instance_url == INSTANCE
    assert "5Aep" not in row.refresh_token and decrypt_secret(row.refresh_token) == "5Aep_refresh_1"
    assert "00D!" not in row.token and decrypt_secret(row.token) == "00D!access_1"
    end = timezone.now() + timedelta(days=7)
    assert end - timedelta(minutes=1) < row.expires_at <= end
    # The access token lives the app's session timeout (1800 s here), far short of the grant.
    assert row.access_expires_at < timezone.now() + timedelta(minutes=31)
    assert not Connection.objects.exists()


def test_a_new_grant_revokes_the_last_refresh_token(w):
    old = _grant(w, refresh="5Aep_old")
    with FakeSF() as fake:
        _granted(w, fake)
    revokes = [c for c in fake.calls if c[1] == REVOKE_URL]
    assert [(c[2]["token"], c[2]["token_type_hint"]) for c in revokes] == [("5Aep_old", "refresh_token")]
    assert TemporaryAccess.objects.get().pk == old.pk  # replaced in place: one per person and app


def test_it_is_never_lent_to_an_agent(w):
    resp = w.client.post(START, {"temporary": "1", "for_agent": "eva", "minutes": "60", "return_to": "/settings"})
    assert "reason=temporary_unsupported" in resp["Location"]


def test_my_connections_lists_it_with_its_end(w):
    row = _grant(w)
    out = w.client.get("/api/connections/").json()["temporary"]
    assert out == [{"id": str(row.pk), "provider": "salesforce", "workspace": "w", "agent": None,
                    "account_label": "jj@example.org", "expires_at": out[0]["expires_at"], "renews": True}]
    assert "5Aep" not in json.dumps(out) and "00D!" not in json.dumps(out)


# ---- a screen's call ----------------------------------------------------------------------------

def test_a_call_runs_on_the_grant(w):
    _grant(w)
    with FakeSF(), Salesforce(Answer(body={"totalSize": 3})) as sf:
        r = _call(w)
    assert r.status_code == 200, r.content
    assert r.json()["body"] == {"totalSize": 3}
    assert sf.calls[0]["url"].startswith(f"{INSTANCE}/services/data/")
    assert _bearer(sf.calls[0]) == "00D!grant_access"


def test_the_grant_comes_before_a_stored_connection(w):
    w.shared(sealed_to_owner({"user": w.person}, w.person, app=w.app, server_use=True),
             canopy_web=server_key.thumbprint())
    _grant(w)
    with FakeSF() as fake, Salesforce() as sf:
        assert _call(w).status_code == 200
    assert _bearer(sf.calls[0]) == "00D!grant_access" and fake.token_calls() == []


def test_without_a_grant_the_stored_connection_canopy_web_may_use_still_works(w):
    w.shared(sealed_to_owner({"user": w.person}, w.person, app=w.app, server_use=True),
             canopy_web=server_key.thumbprint())
    with FakeSF(), Salesforce() as sf:
        assert _call(w).status_code == 200
    assert _bearer(sf.calls[0]) == "00D!access_1"  # the stored connection's, refreshed by server_tokens


def test_with_neither_the_refusal_starts_a_grant(w):
    with Salesforce() as sf:
        r = _call(w)
    assert r.status_code == 409 and r.json()["error"] == "not_connected"
    assert r.json()["connect_url"].endswith("/auth/connections/salesforce/w/start/?temporary=1&for_screens=1")
    assert "Allow Salesforce" in r.json()["detail"] and "Settings" not in r.json()["detail"]
    assert sf.calls == []


def test_a_write_on_the_grant_carries_the_duplicate_flag_and_is_an_event(w):
    _grant(w)
    body = {"method": "PATCH", "path": f"sobjects/Lead/{LEAD}", "body": {"Status": "Open"}, "operation_id": "op-1",
            "allow_duplicates": True,
            "surface": {"kind": "view", "id": w.view.pk, "version": w.view.version}}
    with (mock.patch.object(call_service, "_spend_write") as spent, FakeSF(),
          Salesforce(Answer(status=204, raw=b"")) as sf):
        r = w.client.post(CALL, json.dumps(body), content_type="application/json")
    assert r.status_code == 200, r.content
    assert spent.call_args.args[-1] == 1  # one place on the click's lease
    assert sf.calls[0]["headers"]["Sforce-Duplicate-Rule-Header"] == "allowSave=true"
    assert _bearer(sf.calls[0]) == "00D!grant_access"
    event = Event.objects.get(kind="connections.connection_call")
    assert event.payload["allow_duplicates"] is True and event.payload["status"] == 204
    assert "00D!" not in json.dumps(event.payload) and "5Aep" not in json.dumps(event.payload)


def test_an_access_token_that_ran_out_is_renewed_first(w):
    row = _grant(w, access_in=timedelta(seconds=-5))
    renewed = _resp(body=_token_body(access_token="00D!renewed", refresh_token=None))
    with FakeSF(token=[renewed]) as fake, Salesforce() as sf:
        assert _call(w).status_code == 200
    assert [c[2]["refresh_token"] for c in fake.token_calls()] == ["5Aep_grant_refresh"]
    assert _bearer(sf.calls[0]) == "00D!renewed"
    row.refresh_from_db()
    assert decrypt_secret(row.token) == "00D!renewed" and row.access_expires_at > timezone.now()
    assert decrypt_secret(row.refresh_token) == "5Aep_grant_refresh"  # not rotated: kept


def test_a_rotated_refresh_token_replaces_the_old(w):
    row = _grant(w, access_in=timedelta(seconds=-5))
    rotated = _resp(body=_token_body(access_token="00D!renewed", refresh_token="5Aep_rotated"))
    with FakeSF(token=[rotated]), Salesforce():
        assert _call(w).status_code == 200
    row.refresh_from_db()
    assert decrypt_secret(row.refresh_token) == "5Aep_rotated"


def test_a_401_renews_and_asks_once_more(w):
    _grant(w)
    renewed = _resp(body=_token_body(access_token="00D!renewed", refresh_token=None))
    with FakeSF(token=[renewed]), Salesforce(Answer(status=401, body=[]), Answer(body={"totalSize": 1})) as sf:
        r = _call(w)
    assert r.status_code == 200
    assert [_bearer(c) for c in sf.calls] == ["00D!grant_access", "00D!renewed"]


def test_a_grant_salesforce_no_longer_renews_ends_and_asks_again(w):
    _grant(w, access_in=timedelta(seconds=-5))
    refused = _resp(400, {"error": "invalid_grant", "error_description": "expired access/refresh token"})
    with FakeSF(token=[refused]), Salesforce() as sf:
        r = _call(w)
    assert r.status_code == 409 and r.json()["error"] == "session_ended"
    assert "temporary=1" in r.json()["connect_url"] and "Allow Salesforce" in r.json()["detail"]
    assert sf.calls == [] and not TemporaryAccess.objects.exists()


def test_a_grant_that_ended_is_not_used(w):
    _grant(w, ends_in=timedelta(seconds=-1))
    with Salesforce() as sf:
        r = _call(w)
    assert r.json()["error"] == "not_connected" and sf.calls == []


def test_someone_elses_grant_is_not_mine(w):
    TemporaryAccess.objects.create(
        app=w.app, user=w.ada, provider="salesforce", account_id="00Dorg/005ada", token=encrypt_secret("00D!ada"),
        refresh_token=encrypt_secret("5Aep_ada"), instance_url=INSTANCE, expires_at=timezone.now() + timedelta(days=1))
    with Salesforce() as sf:
        assert _call(w).json()["error"] == "not_connected"
    assert sf.calls == []


# ---- the end ------------------------------------------------------------------------------------

def test_the_sweep_revokes_the_refresh_token_at_salesforce_and_deletes_it(w):
    _grant(w, ends_in=timedelta(seconds=-1))
    with FakeSF() as fake:
        keeper.sweep()
    revokes = [c for c in fake.calls if c[1] == REVOKE_URL]
    assert [(c[2]["token"], c[2]["token_type_hint"]) for c in revokes] == [("5Aep_grant_refresh", "refresh_token")]
    assert not TemporaryAccess.objects.exists()


def test_the_sweep_keeps_one_salesforce_could_not_be_reached_for_and_tries_again(w):
    _grant(w, ends_in=timedelta(seconds=-1))
    with FakeSF(revoke=_resp(503, {"error": "unavailable"})):
        keeper.sweep()
    assert TemporaryAccess.objects.exists()  # used by nothing: it has ended
    assert temporary.for_screens(w.person, "salesforce", "w") is None
    with FakeSF(revoke=_resp(503, {"error": "unavailable"})):
        keeper.sweep(timezone.now() + temporary.REVOKE_RETRY_FOR + timedelta(minutes=1))
    assert not TemporaryAccess.objects.exists()


def test_ending_it_revokes_the_refresh_token_and_takes_no_passkey(w):
    row = _grant(w)
    with FakeSF() as fake:
        resp = w.client.delete(f"/api/connections/temporary/{row.pk}")
    assert resp.status_code == 200 and resp.json() == {"outcome": "revoked"}
    revokes = [c for c in fake.calls if c[1] == REVOKE_URL]
    assert [(c[2]["token"], c[2]["token_type_hint"]) for c in revokes] == [("5Aep_grant_refresh", "refresh_token")]
    assert not TemporaryAccess.objects.exists()


def test_a_shared_agent_call_never_uses_the_viewers_grant(w):
    _grant(w)
    assert temporary.renews("salesforce")
    with mock.patch.object(temporary, "for_screens", wraps=temporary.for_screens) as looked:
        assert call_service._on_temporary(w.person, "salesforce", mock.Mock(workspace="w"), "shared") is False
    looked.assert_not_called()

