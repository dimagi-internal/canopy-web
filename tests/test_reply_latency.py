"""Reply latency: one trace per contact turn, readable per turn and in aggregate, and a
budget that raises an alert when warm turns break it (`apps/harness/latency.py`).

Every hop of a contact turn is recorded where it happens (the site, canopy, the runner,
the reply push), so a slow reply is split into its hops from the turn alone.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.harness import latency, runner_protocol
from apps.harness.alerts import reply_latency
from apps.harness.models import Turn
from apps.harness.schemas import TurnFinishIn
from apps.host_grants import reply_relay
from apps.tokens.models import AppCredential
from tests.fixtures.contact_websocket import _world
from tests.test_reply_push import HOOK, _arrive, _end, _say

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def w(monkeypatch):
    owner, ws, app, priv = _world()
    AppCredential.objects.filter(pk=app.pk).update(reply_webhook_url=HOOK)
    monkeypatch.setattr(reply_relay, "_deliver_soon", lambda pk: None)
    c = Client(HTTP_AUTHORIZATION=f"Bearer {_arrive(priv)}")
    sid = c.post("/api/contact/sessions", {"agent_slug": "echo"}, content_type="application/json").json()["id"]
    return {"owner": owner, "ws": ws, "app": app, "c": c, "sid": sid}


def _send(w, **extra):
    r = w["c"].post(f"/api/contact/sessions/{w['sid']}/send",
                    {"text": "what can you help with?", "client_id": str(uuid.uuid4()), **extra},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    return Turn.objects.get(pk=r.json()["turn_id"])


def _ago(seconds: float) -> dt.datetime:
    return timezone.now() - dt.timedelta(seconds=seconds)


def test_the_site_says_when_it_received_the_message(w):
    received = _ago(9)
    turn = _send(w, received_at=received.isoformat())
    assert latency._parse(turn.latency["site_received"]) == received


@pytest.mark.parametrize("offset", [None, -300, 2 * 86400], ids=["garbage", "future", "two-days-old"])
def test_an_unbelievable_receipt_time_is_ignored_not_refused(w, offset):
    claim = "not a time" if offset is None else _ago(offset).isoformat()
    turn = _send(w, received_at=claim)
    assert "site_received" not in turn.latency


def test_a_send_without_one_still_traces_from_canopys_own_clock(w):
    turn = _send(w)
    assert turn.latency == {}
    t = latency.trace(turn)
    marks = [m["mark"] for m in t["marks"]]
    assert marks[0] == "enqueued" and "site_received" not in marks
    assert "total" not in t["spans"], "no site mark, no site-to-site span"


def test_a_full_turn_is_traced_hop_by_hop(w):
    turn = _send(w, received_at=_ago(30).isoformat())
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING, claimed_at=_ago(20), started_at=_ago(19))
    turn.refresh_from_db()
    _say(turn, "I can help with visits and payments.")
    # The runner reports its own moments on finish; one it may not report is dropped.
    runner_protocol.finish(turn, TurnFinishIn(status="done", latency={
        "warm": True, "prompt_delivered": _ago(12).isoformat(), "first_text": _ago(10).isoformat(),
        "reply_ready": _ago(9).isoformat(), "enqueued": _ago(1000).isoformat(), "x_resolved": "junk"}))
    turn.refresh_from_db()
    assert turn.status == Turn.DONE
    assert turn.latency["warm"] is True
    assert "x_resolved" not in turn.latency
    assert latency._parse(turn.latency.get("enqueued", turn.created_at.isoformat())) > _ago(60), \
        "a runner cannot move canopy's own marks"

    row = reply_relay.on_finished(turn)
    reply_relay.attempt(row, post=lambda url, token: 204)
    turn.refresh_from_db()
    assert {"reply_posted", "site_acknowledged"} <= set(turn.latency)

    t = latency.trace(turn)
    order = [m["mark"] for m in t["marks"]]
    assert order == ["site_received", "enqueued", "claimed", "started", "prompt_delivered", "first_text",
                     "reply_ready", "finished", "reply_posted", "site_acknowledged"]
    assert t["warm"] is True
    assert 29 <= t["spans"]["total"] <= 32
    assert t["spans"]["answer"] == pytest.approx(3, abs=0.5)
    assert t["spans"]["prepare"] == pytest.approx(8, abs=0.5)


def test_a_failed_push_is_not_an_acknowledgement(w):
    turn = _send(w)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.RUNNING)
    turn.refresh_from_db()
    _say(turn, "answer")
    row = reply_relay.on_finished(_end(turn))
    reply_relay.attempt(row, post=lambda url, token: 502)
    turn.refresh_from_db()
    assert "site_acknowledged" not in turn.latency


def test_marks_are_first_write_wins(w):
    turn = _send(w)
    first, later = _ago(5), _ago(1)
    latency.mark(turn.pk, first_text=first)
    latency.mark(turn.pk, first_text=later)     # a retried finish cannot move it
    turn.refresh_from_db()
    assert latency._parse(turn.latency["first_text"]) == first
    latency.mark(uuid.uuid4(), first_text=later)  # no such turn: a no-op, never raises


def test_percentile_is_nearest_rank():
    assert latency.percentile([], 95) is None
    assert latency.percentile([5.0], 95) == 5.0
    values = [float(v) for v in range(1, 21)]
    assert latency.percentile(values, 50) == 10.0
    assert latency.percentile(values, 95) == 19.0


def _traced(w, *, total: float, warm: bool = True, finished_ago: float = 60):
    """A finished contact turn whose site-to-site wait was `total` seconds."""
    turn = _send(w)
    end = _ago(finished_ago)
    Turn.objects.filter(pk=turn.pk).update(
        status=Turn.DONE, claimed_at=end - dt.timedelta(seconds=total / 2), finished_at=end,
        latency={"site_received": (end - dt.timedelta(seconds=total)).isoformat(),
                 "site_acknowledged": end.isoformat(), "warm": warm})
    return turn


def test_the_aggregate_reports_p50_p95_per_runner_kind_and_warmth(w):
    for total in (5, 6, 7, 8, 30):
        _traced(w, total=total)
    _traced(w, total=60, warm=False)
    traces = latency.traces_for(latency.contact_turns(since=_ago(3600)))
    groups = latency.aggregate(traces)
    assert groups["all"]["turns"] == 6
    warm = groups["unknown/warm"]["spans"]["total"]
    assert warm["n"] == 5 and warm["p50"] == pytest.approx(7, abs=0.1) and warm["p95"] == pytest.approx(30, abs=0.1)
    assert groups["unknown/cold"]["spans"]["total"]["max"] == pytest.approx(60, abs=0.1)


def test_the_budget_alert_raises_on_a_slow_warm_window_only(w):
    for total in (5, 6, 7, 8, 9):
        _traced(w, total=total)
    _traced(w, total=90, warm=False)          # a cold start is not this budget's
    assert reply_latency.reconcile(timezone.now()) == []

    for total in (40, 45):
        _traced(w, total=total)
    [r] = reply_latency.reconcile(timezone.now())
    assert r.subject_key == f"reply_latency:{w['ws'].slug}"
    assert "budget 15s" in r.title


def test_too_few_turns_say_nothing_about_a_p95(w):
    for total in (40, 45):
        _traced(w, total=total)
    assert reply_latency.reconcile(timezone.now()) == []


def test_old_turns_leave_the_window(w):
    for total in (40, 41, 42, 43, 44):
        _traced(w, total=total, finished_ago=3 * 86400)
    assert reply_latency.reconcile(timezone.now()) == []
