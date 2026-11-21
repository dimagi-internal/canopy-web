"""Relay channels: a host site carries a person's conversation server-to-server.

A relay arrival is a visitor assertion carrying `canopy_channel`. What these pin:
the token remembers the channel; its turns say `relay:<site>:<channel>` and are the
`site_message` source (which only a relay may name); a relay arrival is always a
contact; and the limits that bound what one site or one person can spend.
"""
from __future__ import annotations

import datetime as dt
import uuid

import jwt
import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings

from apps.canopy_sessions.models import Session
from apps.embedding import assertions
from apps.harness.models import Turn
from apps.tokens.models import AppCredential, ContactToken
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.contact_websocket import _world

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def w():
    owner, ws, app, priv = _world()
    return {"owner": owner, "ws": ws, "app": app, "priv": priv}


def _arrive(priv, sub="u-42", *, agent_slug="echo", status=200, **claims):
    now = int(dt.datetime.now(dt.UTC).timestamp())
    assertion = jwt.encode({"iss": "connect-labs", "sub": sub, "aud": assertions.audience(),
                            "iat": now, "exp": now + 60, "jti": str(uuid.uuid4()), **claims},
                           priv, algorithm="EdDSA")
    r = Client().post("/api/auth/contact-token",
                      data={"assertion": assertion, "agent_slug": agent_slug},
                      content_type="application/json")
    assert r.status_code == status, r.content
    return r.json()


def _relay(priv, sub="u-42", channel="personalid"):
    return _arrive(priv, sub, canopy_channel=channel, canopy_agent="echo")


def _client(raw):
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _open(c):
    r = c.post("/api/contact/sessions", {"agent_slug": "echo"}, content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()["id"]


def test_a_relay_arrival_records_its_channel(w):
    body = _relay(w["priv"])
    assert body["kind"] == "contact"
    token = ContactToken.objects.get(contact_id=body["contact_id"])
    assert token.channel == "personalid"
    assert token.bound_agent.slug == "echo"


def test_a_relay_turn_says_where_it_came_from(w):
    c = _client(_relay(w["priv"])["token"])
    sid = _open(c)
    assert Session.objects.get(pk=sid).metadata["relay_channel"] == "personalid"
    r = c.post(f"/api/contact/sessions/{sid}/send", {"text": "hi"}, content_type="application/json")
    assert r.status_code == 200, r.content
    turn = Turn.objects.get(pk=r.json()["turn_id"])
    assert turn.initiator_via == "relay:connect-labs:personalid"
    assert turn.initiator_kind == "contact"
    assert turn.origin == Turn.ORIGIN_SITE_MESSAGE, "a relay's send is a site message by default"


def test_a_relay_may_name_its_source_and_a_chat_source(w):
    c = _client(_relay(w["priv"])["token"])
    sid = _open(c)
    for origin, expected in (("site_message", "site_message"), ("ace_web", "ace_web")):
        r = c.post(f"/api/contact/sessions/{sid}/send", {"text": origin, "origin": origin},
                   content_type="application/json")
        assert r.status_code == 200, r.content
        assert Turn.objects.get(pk=r.json()["turn_id"]).origin == expected
    r = c.post(f"/api/contact/sessions/{sid}/send", {"text": "x", "origin": "email"},
               content_type="application/json")
    assert r.status_code == 422


def test_a_widget_may_not_name_the_relay_source(w):
    c = _client(_arrive(w["priv"])["token"])
    sid = _open(c)
    assert "relay_channel" not in Session.objects.get(pk=sid).metadata
    r = c.post(f"/api/contact/sessions/{sid}/send", {"text": "hi", "origin": "site_message"},
               content_type="application/json")
    assert r.status_code == 422, r.content
    r = c.post(f"/api/contact/sessions/{sid}/send", {"text": "hi"}, content_type="application/json")
    turn = Turn.objects.get(pk=r.json()["turn_id"])
    assert turn.initiator_via == "widget:connect-labs"
    assert turn.origin != Turn.ORIGIN_SITE_MESSAGE


def test_a_host_cannot_set_the_relay_marker_itself(w):
    c = _client(_arrive(w["priv"])["token"])
    r = c.post("/api/contact/sessions", {"agent_slug": "echo", "metadata": {"relay_channel": "sms"}},
               content_type="application/json")
    assert r.status_code == 200
    assert "relay_channel" not in Session.objects.get(pk=r.json()["id"]).metadata


@pytest.mark.parametrize("channel", ["PersonalID", "has space", "x" * 33, 7])
def test_a_malformed_channel_is_refused(w, channel):
    body = _arrive(w["priv"], canopy_channel=channel, status=400)
    assert "bad_channel" in body["detail"]


def test_a_relay_arrival_is_always_a_contact(w):
    """A member the site may resolve by verified email still arrives as a CONTACT on a
    relay: the person is on the host's channel, not at a canopy browser."""
    AppCredential.objects.filter(pk=w["app"].pk).update(asserts_verified_email=True)
    alice = User.objects.create_user("alice", "alice@dimagi.com", "pw")
    EmailAddress.objects.create(user=alice, email=alice.email, verified=True, primary=True)
    WorkspaceMembership.objects.create(user=alice, workspace=w["ws"], role=WorkspaceMembership.EDITOR)
    widget = _arrive(w["priv"], email="alice@dimagi.com", email_verified=True)
    assert widget["kind"] == "user"
    relay = _arrive(w["priv"], email="alice@dimagi.com", email_verified=True,
                    canopy_channel="personalid", canopy_agent="echo")
    assert relay["kind"] == "contact"


# --- S7: limits ------------------------------------------------------------------------


@override_settings(CONTACT_TOKEN_ISSUER_LIMIT=2)
def test_a_sites_own_token_budget_replaces_the_default(w):
    for i in range(2):
        _relay(w["priv"], sub=f"u-{i}")
    _arrive(w["priv"], "u-9", canopy_channel="personalid", canopy_agent="echo", status=429)
    cache.clear()
    AppCredential.objects.filter(pk=w["app"].pk).update(contact_token_issuer_limit=5)
    for i in range(5):
        _relay(w["priv"], sub=f"v-{i}")
    body = _arrive(w["priv"], "v-9", canopy_channel="personalid", canopy_agent="echo", status=429)
    assert body["detail"].startswith("rate_limited")


@override_settings(CONTACT_SEND_PER_MINUTE=2, CONTACT_SEND_PER_DAY=100)
def test_a_contact_sends_at_most_their_budget_a_minute(w):
    c = _client(_relay(w["priv"])["token"])
    sid = _open(c)
    for i in range(2):
        assert c.post(f"/api/contact/sessions/{sid}/send", {"text": f"m{i}"},
                      content_type="application/json").status_code == 200
    r = c.post(f"/api/contact/sessions/{sid}/send", {"text": "m3"}, content_type="application/json")
    assert r.status_code == 429
    assert r.json()["detail"].startswith("rate_limited")
    assert Turn.objects.filter(chat_session_id=sid).count() == 2, "nothing was written"
    # Another person on the same site is not held up.
    other = _client(_relay(w["priv"], sub="u-43")["token"])
    sid2 = _open(other)
    assert other.post(f"/api/contact/sessions/{sid2}/send", {"text": "hi"},
                      content_type="application/json").status_code == 200


@override_settings(CONTACT_SEND_PER_MINUTE=100, CONTACT_SEND_PER_DAY=1)
def test_a_contact_sends_at_most_their_budget_a_day(w):
    c = _client(_arrive(w["priv"])["token"])
    sid = _open(c)
    assert c.post(f"/api/contact/sessions/{sid}/send", {"text": "a"},
                  content_type="application/json").status_code == 200
    r = c.post(f"/api/contact/sessions/{sid}/send", {"text": "b"}, content_type="application/json")
    assert r.status_code == 429 and "today" in r.json()["detail"]


def test_only_a_superuser_sets_a_sites_token_budget(w):

    owner = Client()
    owner.force_login(w["owner"])
    url = f"/api/workspaces/w1/host-sites/{w['app'].pk}"
    r = owner.patch(url, {"contact_token_issuer_limit": 600}, content_type="application/json")
    assert r.status_code == 403, r.content
    w["owner"].is_superuser = True
    w["owner"].save(update_fields=["is_superuser"])
    r = owner.patch(url, {"contact_token_issuer_limit": 600}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["contact_token_issuer_limit"] == 600
    r = owner.patch(url, {"contact_token_issuer_limit": 0}, content_type="application/json")
    assert r.json()["contact_token_issuer_limit"] is None, "0 puts it back to the default"
    assert AppCredential.objects.get(pk=w["app"].pk).contact_token_issuer_limit is None
