"""Context a host site attests on one message (S3).

The site signs what it knows about this moment with its own key; canopy keeps it on
that turn only and hands it to the agent as the envelope's `host_context`, apart from
the person's words. What these pin: only the site's key, only this conversation and
this person, once; the next turn does not inherit it; retention purges it.
"""
from __future__ import annotations

import datetime as dt
import uuid

import jwt
import pytest
from canopy_sdk import attested_context
from cryptography.hazmat.primitives import serialization
from django.core.cache import cache
from django.test import Client

from apps.embedding import assertions
from apps.harness import caller_context
from apps.harness.models import Turn
from tests.fixtures.contact_websocket import _keypair, _world

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def w():
    owner, ws, app, priv = _world()
    token = _arrive(priv)
    c = Client(HTTP_AUTHORIZATION=f"Bearer {token}")
    sid = c.post("/api/contact/sessions", {"agent_slug": "echo"}, content_type="application/json").json()["id"]
    return {"ws": ws, "app": app, "priv": priv, "c": c, "sid": sid}


def _arrive(priv, sub="personalid:jdoe"):
    now = int(dt.datetime.now(dt.UTC).timestamp())
    assertion = jwt.encode({"iss": "connect-labs", "sub": sub, "aud": assertions.audience(), "iat": now,
                            "exp": now + 60, "jti": str(uuid.uuid4()), "canopy_channel": "personalid",
                            "canopy_agent": "echo"}, priv, algorithm="EdDSA")
    r = Client().post("/api/auth/contact-token", {"assertion": assertion, "agent_slug": "echo"},
                      content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()["token"]


def _key(pem):
    return serialization.load_pem_private_key(pem.encode(), password=None)


def _context(priv, sid, *, sub="personalid:jdoe", fmt="markdown", body="Stage: **learn**", site="connect-labs"):
    return attested_context.sign_context(site, _key(priv), sub=sub, session=sid, format=fmt, body=body)


def _send(w, **body):
    return w["c"].post(f"/api/contact/sessions/{w['sid']}/send", {"text": "when is my visit due?", **body},
                       content_type="application/json")


def test_an_attested_context_reaches_this_turns_envelope_and_no_other(w):
    r = _send(w, context=_context(w["priv"], w["sid"]))
    assert r.status_code == 200, r.content
    turn = Turn.objects.get(pk=r.json()["turn_id"])
    assert turn.origin_ref["host_context"] == {"site": "connect-labs", "format": "markdown",
                                               "body": "Stage: **learn**"}
    assert turn.prompt == "when is my visit due?", "the host's words never join the person's"
    envelope = caller_context.build(turn)
    assert envelope["host_context"] == {"site": "connect-labs", "format": "markdown",
                                        "body": "Stage: **learn**", "attested": True}
    nxt = Turn.objects.get(pk=_send(w, client_id="2").json()["turn_id"])
    assert caller_context.build(nxt)["host_context"] is None


def test_a_json_context(w):
    r = _send(w, context=_context(w["priv"], w["sid"], fmt="json", body='{"stage": "learn"}'))
    assert r.status_code == 200, r.content
    assert Turn.objects.get(pk=r.json()["turn_id"]).origin_ref["host_context"]["format"] == "json"


def test_another_persons_context_is_a_mismatch(w):
    r = _send(w, context=_context(w["priv"], w["sid"], sub="personalid:someone-else"))
    assert r.status_code == 422
    assert r.json()["detail"].startswith("context_mismatch")
    assert not Turn.objects.filter(chat_session_id=w["sid"]).exists(), "nothing was sent"


def test_another_conversations_context_is_a_mismatch(w):
    r = _send(w, context=_context(w["priv"], str(uuid.uuid4())))
    assert r.status_code == 422 and r.json()["detail"].startswith("context_mismatch")


def test_only_the_sites_own_key_attests(w):
    other, _pub = _keypair()
    r = _send(w, context=_context(other, w["sid"]))
    assert r.status_code == 422 and r.json()["detail"].startswith("bad_context")


def test_another_sites_name_is_refused(w):
    r = _send(w, context=_context(w["priv"], w["sid"], site="ace-web"))
    assert r.status_code == 422 and "bad_issuer" in r.json()["detail"]


def test_a_context_is_used_once(w):
    token = _context(w["priv"], w["sid"])
    assert _send(w, context=token, client_id="a").status_code == 200
    r = _send(w, context=token, client_id="b")
    assert r.status_code == 422 and "replayed" in r.json()["detail"]


def test_retention_purges_the_context_with_the_prompt(w):
    from apps.retention import services as retention

    turn = Turn.objects.get(pk=_send(w, context=_context(w["priv"], w["sid"])).json()["turn_id"])
    retention._scrub_turns([turn.pk], dt.datetime.now(dt.UTC))
    turn.refresh_from_db()
    assert "host_context" not in turn.origin_ref
    assert turn.origin_ref["chat_session_id"] == w["sid"], "what routes the turn stays"
