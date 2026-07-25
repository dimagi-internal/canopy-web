"""SP2a Task 5 — the /api/canopy-sessions surface: create, get, send (stub inline), tenancy."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture()
def ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    return user, ws, agent


@pytest.fixture()
def client(ctx):
    c = Client()
    c.force_login(ctx[0])
    return c


def test_create_and_get_empty_session(client):
    r = client.post("/api/canopy-sessions/", data={"agent_slug": "echo", "title": "T"}, content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["agent_slug"] == "echo"
    sid = body["id"]

    detail = client.get(f"/api/canopy-sessions/{sid}")
    assert detail.status_code == 200
    assert detail.json()["messages"] == []


def test_send_runs_stub_and_transcript_appears(client):
    sid = client.post("/api/canopy-sessions/", data={"agent_slug": "echo"}, content_type="application/json").json()["id"]
    r = client.post(f"/api/canopy-sessions/{sid}/send", data={"text": "hi"}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["message"]["role"] == "user"

    detail = client.get(f"/api/canopy-sessions/{sid}").json()
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]  # the stub executed and projected


def test_empty_text_rejected(client):
    sid = client.post("/api/canopy-sessions/", data={}, content_type="application/json").json()["id"]
    r = client.post(f"/api/canopy-sessions/{sid}/send", data={"text": "   "}, content_type="application/json")
    assert r.status_code == 422


def test_non_member_gets_404(client):
    other = User.objects.create_user("no", "no@dimagi.com", "pw")
    ws2 = Workspace.objects.create(slug="other", display_name="Other", created_by=other)
    WorkspaceMembership.objects.create(user=other, workspace=ws2, role=WorkspaceMembership.OWNER)
    foreign = Session.objects.create(workspace=ws2, created_by=other)
    r = client.get(f"/api/canopy-sessions/{foreign.id}")
    assert r.status_code == 404


def test_create_project_session(client):
    r = client.post("/api/canopy-sessions/", data={"project": "canopy-web"}, content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["agent_slug"] is None
    assert body["project"] == "canopy-web"


def test_create_rejects_agent_and_project_together(client):
    r = client.post(
        "/api/canopy-sessions/",
        data={"agent_slug": "echo", "project": "canopy-web"},
        content_type="application/json",
    )
    assert r.status_code == 422, r.content


# --- Task 9: directed session placement -----------------------------------


def test_create_with_runner_id_stashes_requested_runner_id(client, ctx):
    user, _ws, _agent = ctx
    runner = Runner.objects.create(
        name="r1", kind=Runner.EMDASH, capabilities={"sessions": True}, paired_by=user,
    )
    r = client.post(
        "/api/canopy-sessions/",
        data={"project": "canopy-web", "runner_id": str(runner.id)},
        content_type="application/json",
    )
    assert r.status_code == 200, r.content
    sid = r.json()["id"]
    session = Session.objects.get(pk=sid)
    assert session.metadata["requested_runner_id"] == str(runner.id)


def test_send_with_placement_pins_the_turn(client, ctx):
    user, _ws, _agent = ctx
    runner = Runner.objects.create(
        name="r1", kind=Runner.EMDASH, capabilities={"sessions": True}, paired_by=user,
    )
    sid = client.post("/api/canopy-sessions/", data={"agent_slug": "echo"}, content_type="application/json").json()["id"]
    r = client.post(
        f"/api/canopy-sessions/{sid}/send",
        data={"text": "hi", "placement": str(runner.id)},
        content_type="application/json",
    )
    assert r.status_code == 200, r.content
    turn_id = r.json()["turn_id"]
    turn = Turn.objects.get(pk=turn_id)
    assert turn.pinned_runner_id == runner.id


def test_send_with_unknown_placement_is_422(client, ctx):
    sid = client.post("/api/canopy-sessions/", data={"agent_slug": "echo"}, content_type="application/json").json()["id"]
    r = client.post(
        f"/api/canopy-sessions/{sid}/send",
        data={"text": "hi", "placement": "00000000-0000-0000-0000-000000000000"},
        content_type="application/json",
    )
    assert r.status_code == 422, r.content


def test_place_repins_queued_turn(client, ctx, settings):
    # place_queued_turn only matches a still-QUEUED turn — disable the inline
    # stub (which would otherwise run the turn to DONE synchronously) so the
    # send's turn is still there to re-pin.
    settings.CHAT_STUB_EXECUTOR = False
    user, _ws, _agent = ctx
    r1 = Runner.objects.create(
        name="r1", kind=Runner.EMDASH, capabilities={"sessions": True}, paired_by=user,
    )
    r2 = Runner.objects.create(
        name="r2", kind=Runner.EMDASH, capabilities={"sessions": True}, paired_by=user,
    )
    sid = client.post("/api/canopy-sessions/", data={"agent_slug": "echo"}, content_type="application/json").json()["id"]
    RunnerBinding.objects.create(session_id=sid, runner=r1, thread_key=sid)
    client.post(f"/api/canopy-sessions/{sid}/send", data={"text": "hi", "placement": "wait"}, content_type="application/json")

    r = client.post(
        f"/api/canopy-sessions/{sid}/place", data={"placement": str(r2.id)}, content_type="application/json",
    )
    assert r.status_code == 200, r.content
    assert r.json()["pinned_runner_id"] == str(r2.id)


def test_send_with_foreign_tenant_placement_is_422(client, ctx):
    # A runner paired by a user who is NOT a member of this session's workspace
    # is not a valid placement target (it could never claim the resulting
    # turn) — must 422 exactly like an unknown runner id, not silently pin a
    # turn that becomes permanently unclaimable.
    user, _ws, _agent = ctx
    outsider = User.objects.create_user("outsider", "outsider@dimagi.com", "pw")
    foreign_runner = Runner.objects.create(
        name="foreign", kind=Runner.EMDASH, capabilities={"sessions": True}, paired_by=outsider,
    )
    sid = client.post("/api/canopy-sessions/", data={"agent_slug": "echo"}, content_type="application/json").json()["id"]
    r = client.post(
        f"/api/canopy-sessions/{sid}/send",
        data={"text": "hi", "placement": str(foreign_runner.id)},
        content_type="application/json",
    )
    assert r.status_code == 422, r.content
    assert not Turn.objects.filter(pinned_runner=foreign_runner).exists()


def test_send_with_malformed_placement_is_422(client, ctx):
    # A non-UUID placement string must 422, not 500 — django's ValidationError
    # from a raw string ORM lookup must never leak past _placeable_runner.
    sid = client.post("/api/canopy-sessions/", data={"agent_slug": "echo"}, content_type="application/json").json()["id"]
    r = client.post(
        f"/api/canopy-sessions/{sid}/send",
        data={"text": "hi", "placement": "banana"},
        content_type="application/json",
    )
    assert r.status_code == 422, r.content


def test_place_with_malformed_placement_is_422(client, ctx, settings):
    settings.CHAT_STUB_EXECUTOR = False
    sid = client.post("/api/canopy-sessions/", data={"agent_slug": "echo"}, content_type="application/json").json()["id"]
    client.post(f"/api/canopy-sessions/{sid}/send", data={"text": "hi"}, content_type="application/json")
    r = client.post(
        f"/api/canopy-sessions/{sid}/place", data={"placement": "banana"}, content_type="application/json",
    )
    assert r.status_code == 422, r.content


def test_send_with_sessions_incapable_placement_is_422(client, ctx):
    # A pin can direct a chat turn to a runner that isn't sessions-capable
    # unless the server rejects it — such a runner could claim the turn (pins
    # bypass target/routing matching) but could never bridge the reply back.
    user, _ws, _agent = ctx
    incapable = Runner.objects.create(
        name="incapable", kind=Runner.EMDASH, capabilities={}, paired_by=user,
    )
    sid = client.post("/api/canopy-sessions/", data={"agent_slug": "echo"}, content_type="application/json").json()["id"]
    r = client.post(
        f"/api/canopy-sessions/{sid}/send",
        data={"text": "hi", "placement": str(incapable.id)},
        content_type="application/json",
    )
    assert r.status_code == 422, r.content
    assert not Turn.objects.filter(pinned_runner=incapable).exists()


def test_place_with_no_queued_turn_is_404(client):
    sid = client.post("/api/canopy-sessions/", data={"agent_slug": "echo"}, content_type="application/json").json()["id"]
    r = client.post(
        f"/api/canopy-sessions/{sid}/place", data={"placement": "wait"}, content_type="application/json",
    )
    assert r.status_code == 404, r.content


# --- Task 9 (chat-embed-polish): session list filters for embedders --------


def test_list_sessions_filters_by_metadata(client, ctx):
    user, ws, _agent = ctx
    Session.objects.create(
        workspace=ws, created_by=user, metadata={"source": "ace-web", "opp_slug": "field-hep"},
    )
    Session.objects.create(workspace=ws, created_by=user, metadata={"source": "ace-web"})
    Session.objects.create(workspace=ws, created_by=user, metadata={})

    r = client.get("/api/canopy-sessions/?source=ace-web")
    assert r.status_code == 200, r.content
    assert len(r.json()) == 2

    r = client.get("/api/canopy-sessions/?source=ace-web&opp_slug=field-hep")
    assert r.status_code == 200, r.content
    assert len(r.json()) == 1

    r = client.get("/api/canopy-sessions/")
    assert r.status_code == 200, r.content
    assert len(r.json()) == 3


# --- Task 11 fix wave: title vs bound session_key in _out --------------------


def test_web_origin_session_with_title_reports_own_title(client, ctx):
    # A web-origin session that has been auto-titled (Task 10) must show that
    # title in the chat index — even once a runner binding forms underneath it
    # to execute the turn — not the bound emdash task name.
    user, _ws, _agent = ctx
    sid = client.post(
        "/api/canopy-sessions/", data={"agent_slug": "echo"}, content_type="application/json"
    ).json()["id"]
    session = Session.objects.get(pk=sid)
    session.title = "Help me plan the Q3 field visit schedule"
    session.save(update_fields=["title"])
    runner = Runner.objects.create(
        name="r1", kind=Runner.EMDASH, capabilities={"sessions": True}, paired_by=user,
    )
    RunnerBinding.objects.create(session_id=sid, runner=runner, thread_key=sid, session_key="ace-api-1a2b-cdef")

    detail = client.get(f"/api/canopy-sessions/{sid}").json()
    assert detail["title"] == "Help me plan the Q3 field visit schedule"

    listed = client.get("/api/canopy-sessions/").json()
    assert next(s for s in listed if s["id"] == sid)["title"] == "Help me plan the Q3 field visit schedule"


def test_runner_origin_session_still_reports_task_name(client, ctx):
    # Runner-DISCOVERED sessions never get a human-authored title — showing the
    # emdash task name (not a raw thread-key hash) is still correct here.
    user, ws, _agent = ctx
    session = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_RUNNER, title="")
    runner = Runner.objects.create(
        name="r1", kind=Runner.EMDASH, capabilities={"sessions": True}, paired_by=user,
    )
    RunnerBinding.objects.create(
        session_id=session.id, runner=runner, thread_key=str(session.id), session_key="ace-api-1a2b-cdef",
    )

    detail = client.get(f"/api/canopy-sessions/{session.id}").json()
    assert detail["title"] == "ace-api-1a2b-cdef"
# --- Task 6: chat.stop's REST twin ------------------------------------------


def test_stop_cancels_queued_turn(client):
    sid = client.post("/api/canopy-sessions/", data={"agent_slug": "echo"}, content_type="application/json").json()["id"]
    turn = Turn.objects.create(
        chat_session_id=sid, origin=Turn.ORIGIN_API, idempotency_key="q1", status=Turn.QUEUED
    )
    r = client.post(f"/api/canopy-sessions/{sid}/stop", content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json() == {"cancelled": True}
    turn.refresh_from_db()
    assert turn.status == Turn.CANCELLED


def test_stop_cancels_every_non_terminal_turn_not_just_the_newest(client):
    # I1: a mid-reply send queues turn B behind still-running turn A. Stop must
    # reach BOTH — B (queued) finishes CANCELLED, A (running) gets a
    # cancel_requested ledger event so its runner is signalled to interrupt.
    sid = client.post("/api/canopy-sessions/", data={"agent_slug": "echo"}, content_type="application/json").json()["id"]
    turn_a = Turn.objects.create(
        chat_session_id=sid, origin=Turn.ORIGIN_API, idempotency_key="a1", status=Turn.RUNNING,
    )
    turn_b = Turn.objects.create(
        chat_session_id=sid, origin=Turn.ORIGIN_API, idempotency_key="b1", status=Turn.QUEUED,
    )
    r = client.post(f"/api/canopy-sessions/{sid}/stop", content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json() == {"cancelled": True}

    turn_a.refresh_from_db()
    turn_b.refresh_from_db()
    assert turn_a.status == Turn.RUNNING  # untouched — runner owns the lease
    assert turn_a.events.filter(kind="cancel_requested").exists()
    assert turn_b.status == Turn.CANCELLED


def test_stop_with_nothing_to_cancel_returns_false(client):
    sid = client.post("/api/canopy-sessions/", data={"agent_slug": "echo"}, content_type="application/json").json()["id"]
    r = client.post(f"/api/canopy-sessions/{sid}/stop", content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json() == {"cancelled": False}
