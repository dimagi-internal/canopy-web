"""A host's work FOR a contact — the same things it does for a user while
executing their command (ace-web's runs): send routed as the host's source,
stop, read a turn, read its transcript, ask what no runner can take. Over the
contact's OWN conversations only.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import Session
from apps.harness import services as harness
from apps.harness.models import Turn
from tests.test_contact_websocket import _contact_token, _world

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def w():
    owner, ws, app, priv = _world()
    me = Client(HTTP_AUTHORIZATION=f"Bearer {_contact_token(priv, sub='neal')}")
    other = Client(HTTP_AUTHORIZATION=f"Bearer {_contact_token(priv, sub='someone-else')}")
    sid = me.post("/api/contact/sessions", {"agent_slug": "echo", "metadata": {"opp_slug": "bednets"}},
                  content_type="application/json").json()["id"]
    return {"me": me, "other": other, "sid": sid, "ws": ws}


def _send(c, sid, **extra):
    return c.post(f"/api/contact/sessions/{sid}/send", {"text": "run it", **extra},
                  content_type="application/json")


def test_a_hosts_run_for_a_contact_routes_as_the_hosts_source(w):
    r = _send(w["me"], w["sid"], origin="ace_web")
    assert r.status_code == 200, r.content
    assert Turn.objects.get(pk=r.json()["turn_id"]).origin == Turn.ORIGIN_ACE_WEB


@pytest.mark.parametrize("origin", ["email", "slack", "canopy_scheduler", "nonsense"])
def test_a_contact_cannot_claim_an_attested_channel(w, origin):
    assert _send(w["me"], w["sid"], origin=origin).status_code == 422


def test_reading_a_turn_and_its_transcript(w):
    tid = _send(w["me"], w["sid"]).json()["turn_id"]
    t = w["me"].get(f"/api/contact/turns/{tid}")
    assert t.status_code == 200 and t.json()["id"] == tid
    tr = w["me"].get(f"/api/contact/turns/{tid}/transcript")
    assert tr.status_code == 200 and tr["Content-Type"] == "application/x-ndjson"


def test_stop_cancels_my_unfinished_turns(w):
    Turn.objects.filter(chat_session_id=w["sid"]).delete()
    t, _ = harness.enqueue_turn(session=Session.objects.get(pk=w["sid"]), origin=Turn.ORIGIN_API,
                                idempotency_key="q1", prompt="x")
    r = w["me"].post(f"/api/contact/sessions/{w['sid']}/stop")
    assert r.status_code == 200 and r.json()["cancelled"]
    t.refresh_from_db()
    assert t.status == Turn.CANCELLED


def test_what_no_runner_can_take_is_mine_only(w):
    s = Session.objects.get(pk=w["sid"])
    t, _ = harness.enqueue_turn(session=s, origin=Turn.ORIGIN_API, idempotency_key="stuck", prompt="x")
    Turn.objects.filter(pk=t.pk).update(created_at=timezone.now() - timedelta(hours=1))
    theirs = Session.objects.create(workspace=w["ws"], title="not mine")
    t2, _ = harness.enqueue_turn(session=theirs, origin=Turn.ORIGIN_API, idempotency_key="other", prompt="x")
    Turn.objects.filter(pk=t2.pk).update(created_at=timezone.now() - timedelta(hours=1))
    rows = w["me"].get("/api/contact/turns/unclaimable").json()
    assert [r["turn_id"] for r in rows] == [str(t.pk)]


@pytest.mark.parametrize("path", ["/api/contact/turns/{tid}", "/api/contact/turns/{tid}/transcript"])
def test_another_contact_cannot_read_my_turn(w, path):
    tid = _send(w["me"], w["sid"]).json()["turn_id"]
    assert w["other"].get(path.format(tid=tid)).status_code == 404


def test_another_contact_cannot_stop_my_conversation(w):
    assert w["other"].post(f"/api/contact/sessions/{w['sid']}/stop").status_code == 404


def test_a_bad_turn_id_is_not_found_not_an_error(w):
    assert w["me"].get("/api/contact/turns/not-a-uuid").status_code == 404
