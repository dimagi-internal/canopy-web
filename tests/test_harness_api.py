"""API-level tests for /api/harness (runner pairing, claim loop, turn lifecycle)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import HEARTBEAT_ONLINE_WINDOW, Runner, RunnerAssignment

pytestmark = pytest.mark.django_db


@pytest.fixture()
def client():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture()
def agent():
    return Agent.objects.create(slug="echo", name="Echo")


def _pair(client) -> str:
    resp = client.post(
        "/api/harness/runners/",
        {"name": "jj-mbp", "kind": "emdash", "capabilities": {"agents": ["echo"]}},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    return resp.json()["id"]


def _hb(client, rid, active=None, degraded=False):
    return client.post(
        f"/api/harness/runners/{rid}/heartbeat",
        {"active_turn_ids": active or [], "degraded": degraded, "note": ""},
        content_type="application/json",
    )


def test_pair_heartbeat_claim_cycle(client, agent):
    rid = _pair(client)
    assert _hb(client, rid).status_code == 200
    RunnerAssignment.objects.create(agent=agent, runner_id=rid, rank=0)

    enq = client.post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "manual", "idempotency_key": "k1"},
        content_type="application/json",
    )
    assert enq.status_code == 201

    claim = client.post(f"/api/harness/runners/{rid}/claim")
    assert claim.status_code == 200
    turn = claim.json()
    assert turn["status"] == "claimed" and turn["agent_slug"] == "echo"

    again = client.post(f"/api/harness/runners/{rid}/claim")
    assert again.status_code == 204


def test_enqueue_replays_on_same_key(client, agent):
    body = {"agent_slug": "echo", "origin": "manual", "idempotency_key": "k1"}
    first = client.post("/api/harness/turns/", body, content_type="application/json")
    second = client.post("/api/harness/turns/", body, content_type="application/json")
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_enqueue_stacks_behind_busy_lane(client, agent):
    client.post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "manual", "idempotency_key": "k1"},
        content_type="application/json",
    )
    resp = client.post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "slack", "idempotency_key": "k2"},
        content_type="application/json",
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "queued"


def test_event_append_and_cursor_read(client, agent):
    enq = client.post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "manual", "idempotency_key": "k1"},
        content_type="application/json",
    ).json()
    tid = enq["id"]
    resp = client.post(
        f"/api/harness/turns/{tid}/events",
        {"events": [{"kind": "status", "payload": {"s": "x"}}, {"kind": "assistant", "payload": {"text": "hi"}}]},
        content_type="application/json",
    )
    assert resp.status_code == 200 and resp.json()["count"] == 2
    # enqueue itself writes no event (services.enqueue_turn is silent);
    # our two posted events get seq 1 and 2.
    events = client.get(f"/api/harness/turns/{tid}/events?after=0").json()["events"]
    assert [e["seq"] for e in events] == [1, 2]


def test_start_and_finish(client, agent):
    rid = _pair(client)
    assert _hb(client, rid).status_code == 200
    RunnerAssignment.objects.create(agent=agent, runner_id=rid, rank=0)
    tid = client.post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "manual", "idempotency_key": "k1"},
        content_type="application/json",
    ).json()["id"]
    claimed = client.post(f"/api/harness/runners/{rid}/claim")
    assert claimed.status_code == 200 and claimed.json()["id"] == tid
    started = client.post(
        f"/api/harness/turns/{tid}/start", {"session_id": "abc"}, content_type="application/json"
    )
    assert started.status_code == 200 and started.json()["status"] == "running"
    finished = client.post(
        f"/api/harness/turns/{tid}/finish",
        {"status": "done", "result_note": "2 applied"},
        content_type="application/json",
    )
    assert finished.status_code == 200 and finished.json()["status"] == "done"


def test_finish_with_cancelled_status_is_accepted(client, agent):
    # C1: the runner's own cancel_turn interrupt calls client.finish(...,
    # status="cancelled") — the finish route must accept it, not 422 (which
    # would bounce back as a ClientError and land the turn FAILED, flipping
    # readiness.mark_failed on a runner that behaved correctly).
    rid = _pair(client)
    assert _hb(client, rid).status_code == 200
    RunnerAssignment.objects.create(agent=agent, runner_id=rid, rank=0)
    tid = client.post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "manual", "idempotency_key": "k1"},
        content_type="application/json",
    ).json()["id"]
    claimed = client.post(f"/api/harness/runners/{rid}/claim")
    assert claimed.status_code == 200 and claimed.json()["id"] == tid
    finished = client.post(
        f"/api/harness/turns/{tid}/finish",
        {"status": "cancelled", "result_note": "interrupted"},
        content_type="application/json",
    )
    assert finished.status_code == 200, finished.content
    assert finished.json()["status"] == "cancelled"


def test_finish_on_queued_turn_is_409(client, agent):
    tid = client.post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "manual", "idempotency_key": "k1"},
        content_type="application/json",
    ).json()["id"]
    resp = client.post(
        f"/api/harness/turns/{tid}/finish",
        {"status": "done", "result_note": "n/a"},
        content_type="application/json",
    )
    assert resp.status_code == 409, resp.content


def test_list_filter_by_agent_and_status(client, agent):
    # the exact query the drain-turn skill issues
    client.post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "manual", "idempotency_key": "k1"},
        content_type="application/json",
    )
    resp = client.get("/api/harness/turns/?agent=echo&status=claimed,running,queued")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1 and body[0]["agent_slug"] == "echo"
    empty = client.get("/api/harness/turns/?agent=echo&status=done")
    assert empty.status_code == 200 and empty.json() == []


def test_list_respects_limit_and_clamps(client, agent):
    # enqueue 3 turns for the same agent (idempotency_key must differ per row)
    for i in range(3):
        r = client.post(
            "/api/harness/turns/",
            {"agent_slug": "echo", "origin": "manual", "idempotency_key": f"k{i}"},
            content_type="application/json",
        )
        assert r.status_code == 201, r.content

    # limit caps the row count
    assert len(client.get("/api/harness/turns/?limit=2").json()) == 2
    # limit below 1 clamps up to 1 (never returns 0 / everything)
    assert len(client.get("/api/harness/turns/?limit=0").json()) == 1
    # limit above 200 clamps down to 200 (here just <= the 3 we have)
    assert len(client.get("/api/harness/turns/?limit=9999").json()) == 3
    # omitted -> default 100 (existing callers unchanged): returns all 3
    assert len(client.get("/api/harness/turns/").json()) == 3


def test_anonymous_is_401(agent):
    c = Client()
    resp = c.get("/api/harness/turns/")
    assert resp.status_code == 401


def test_malformed_turn_id_is_422(client, agent):
    resp = client.get("/api/harness/turns/not-a-uuid")
    assert resp.status_code == 422, resp.content


def test_malformed_runner_id_is_422(client, agent):
    resp = client.post("/api/harness/runners/not-a-uuid/claim")
    assert resp.status_code == 422, resp.content


def test_unknown_event_kind_is_422(client, agent):
    enq = client.post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "manual", "idempotency_key": "k1"},
        content_type="application/json",
    ).json()
    tid = enq["id"]
    resp = client.post(
        f"/api/harness/turns/{tid}/events",
        {"events": [{"kind": "bogus_kind", "payload": {}}]},
        content_type="application/json",
    )
    assert resp.status_code == 422, resp.content


def test_pair_with_host_and_resolve_record_cycle(client, agent):
    # agent fixture is "echo"; pair a runner capable of echo with a macOS host
    resp = client.post(
        "/api/harness/runners/",
        {"name": "jj-mbp", "kind": "emdash", "capabilities": {"agents": ["echo"]}, "host": "jjA@mbp"},
        content_type="application/json",
    )
    assert resp.status_code == 201 and resp.json()["host"] == "jjA@mbp"
    rid = resp.json()["id"]

    # unknown thread -> new_thread, no reuse
    r = client.post(f"/api/harness/runners/{rid}/resolve-session",
                    {"agent_slug": "echo", "thread_key": "thr-1"}, content_type="application/json")
    assert r.status_code == 200 and r.json()["new_thread"] is True and r.json()["reuse"] is False

    # record a live session for the thread on this runner
    rec = client.post(f"/api/harness/runners/{rid}/record-session",
                      {"agent_slug": "echo", "thread_key": "thr-1", "emdash_task_id": "etask-1",
                       "session_id": "sess-1", "agent_task_ext_id": "T-9", "summary": "ctx"},
                      content_type="application/json")
    assert rec.status_code == 200 and rec.json()["reuse"] is True
    assert rec.json()["emdash_task_id"] == "etask-1"

    # same runner resolves -> reuse
    again = client.post(f"/api/harness/runners/{rid}/resolve-session",
                        {"agent_slug": "echo", "thread_key": "thr-1"}, content_type="application/json")
    assert again.json()["reuse"] is True and again.json()["agent_task_ext_id"] == "T-9"


def test_other_account_runner_cannot_reuse_but_gets_context(client, agent):
    a = client.post("/api/harness/runners/",
                    {"name": "rA", "kind": "emdash", "capabilities": {"agents": ["echo"]}, "host": "jjA@mbp"},
                    content_type="application/json").json()["id"]
    b = client.post("/api/harness/runners/",
                    {"name": "rB", "kind": "emdash", "capabilities": {"agents": ["echo"]}, "host": "jjB@mbp"},
                    content_type="application/json").json()["id"]
    client.post(f"/api/harness/runners/{a}/record-session",
                {"agent_slug": "echo", "thread_key": "thr-1", "emdash_task_id": "etask-A",
                 "summary": "prior"}, content_type="application/json")
    r = client.post(f"/api/harness/runners/{b}/resolve-session",
                    {"agent_slug": "echo", "thread_key": "thr-1"}, content_type="application/json").json()
    assert r["reuse"] is False and r["new_thread"] is False and r["summary"] == "prior"


def test_list_runners_returns_my_runners_newest_heartbeat_first(client, agent):
    from datetime import timedelta

    from django.utils import timezone

    # Newest: heartbeats now (via the API, so status/host get exercised too).
    newest = _pair(client)
    _hb(client, newest)

    # Older: heartbeat an hour ago.
    older = _pair(client)
    _hb(client, older)
    Runner.objects.filter(pk=older).update(last_heartbeat_at=timezone.now() - timedelta(hours=1))

    # Never heartbeated: last_heartbeat_at stays null — must sort last (nulls_last=True).
    never = _pair(client)

    resp = client.get("/api/harness/runners/")
    assert resp.status_code == 200
    body = resp.json()
    assert [r["id"] for r in body] == [newest, older, never]
    assert body[0]["status"] == "online"
    assert body[0]["host"] == ""


def test_list_runners_excludes_retired(client, agent):
    rid = _pair(client)
    Runner.objects.filter(pk=rid).update(status=Runner.RETIRED)
    assert client.get("/api/harness/runners/").json() == []


# --------------------------------------------------------------------------------------
# Runner.live_status — the column says what the runner last claimed; the API must serve
# what we can actually OBSERVE (heartbeat age), not the raw column. See models.py.
# --------------------------------------------------------------------------------------

def test_stale_heartbeat_reports_stale_even_though_column_says_online(client, agent):
    rid = _pair(client)
    assert _hb(client, rid).status_code == 200
    # Push the last heartbeat outside the window without going through heartbeat()
    # again — the stored column is untouched and still says "online".
    old = timezone.now() - HEARTBEAT_ONLINE_WINDOW - timedelta(seconds=1)
    Runner.objects.filter(pk=rid).update(last_heartbeat_at=old)

    stored = Runner.objects.get(pk=rid)
    assert stored.status == Runner.ONLINE  # the lie: nothing ever demoted the column

    body = client.get("/api/harness/runners/").json()
    assert len(body) == 1 and body[0]["status"] == "stale"  # the API tells the truth


def test_never_heartbeated_reports_disconnected_regardless_of_stored_status(client, agent):
    rid = _pair(client)
    # Force the column to ONLINE directly (never call heartbeat()) so this test
    # exercises the "no heartbeat at all" branch distinctly from the column value —
    # last_heartbeat_at stays None throughout.
    Runner.objects.filter(pk=rid).update(status=Runner.ONLINE)
    assert Runner.objects.get(pk=rid).last_heartbeat_at is None

    body = client.get("/api/harness/runners/").json()
    assert len(body) == 1 and body[0]["status"] == "disconnected"


def test_fresh_heartbeat_reports_online(client, agent):
    rid = _pair(client)
    assert _hb(client, rid).status_code == 200
    body = client.get("/api/harness/runners/").json()
    assert len(body) == 1 and body[0]["status"] == "online"


def test_degraded_and_fresh_stays_degraded(client, agent):
    rid = _pair(client)
    resp = _hb(client, rid, degraded=True)
    assert resp.status_code == 200 and resp.json()["status"] == "degraded"
    body = client.get("/api/harness/runners/").json()
    assert len(body) == 1 and body[0]["status"] == "degraded"  # not clobbered to online


def test_retired_runner_404s_on_gated_routes(client, agent):
    rid = _pair(client)
    Runner.objects.filter(pk=rid).update(status=Runner.RETIRED)
    resp = client.post(
        f"/api/harness/runners/{rid}/heartbeat",
        {"active_turn_ids": [], "degraded": False, "note": ""},
        content_type="application/json",
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------------------
# POST /runners/{id}/retire
# --------------------------------------------------------------------------------------

def test_retire_runner_removes_it_from_the_list(client, agent):
    rid = _pair(client)
    resp = client.post(f"/api/harness/runners/{rid}/retire")
    assert resp.status_code == 204
    assert Runner.objects.get(pk=rid).status == Runner.RETIRED
    assert client.get("/api/harness/runners/").json() == []


def test_retiring_an_already_retired_runner_404s(client, agent):
    """Idempotent by construction: _runner_or_404 excludes retired runners at
    lookup, so a second retire is a 404, not a no-op 204 — the existing
    lookup behaviour, not a special case added for this route."""
    rid = _pair(client)
    assert client.post(f"/api/harness/runners/{rid}/retire").status_code == 204
    assert client.post(f"/api/harness/runners/{rid}/retire").status_code == 404


def test_enqueue_records_the_human_who_launched_the_turn(client, agent):
    enq = client.post(
        "/api/harness/turns/",
        {"agent_slug": "echo", "origin": "manual", "idempotency_key": "who"},
        content_type="application/json",
    )
    assert enq.status_code == 201
    assert enq.json()["enqueued_by_email"] == "jj@dimagi.com"
    from apps.harness.models import Turn
    assert Turn.objects.get(idempotency_key="who").enqueued_by.email == "jj@dimagi.com"
