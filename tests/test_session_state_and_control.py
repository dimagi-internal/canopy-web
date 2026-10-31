"""One answer to "what is this session doing", and a durable record behind every
verb a runner must carry out.

* `session_state.is_running`: a turn that drives the session in flight, or (no
  turn behind the work) the engine's flag — so a cloud session reads as running
  in the list and the feed, where only the laptop's engine flag used to count.
* Stop records `RunnerBinding.pending_interrupt`, drained by the runner.
* A transfer holds the session's other turns until its own turn has started.
* Every send that ended without landing is listed with its final state.
* Archive cancels open turns and holds against the laptop's report; a send
  brings an archived session back.
"""
from __future__ import annotations

import datetime as dt
import time
import uuid
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions import control, feed, session_state, staleness
from apps.canopy_sessions import placement as session_placement
from apps.canopy_sessions import send as session_send
from apps.canopy_sessions.models import Message, RunnerBinding, Session, SessionHandoff
from apps.harness import claim, routing, session_sync
from apps.harness import turn_status as ts
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.testing import enrolled

pytestmark = pytest.mark.django_db

CAPS = {"sessions": True, "projects": ["canopy-web"]}


@pytest.fixture
def ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    now = timezone.now()
    laptop = enrolled(Runner.objects.create(
        name="jj-mbp-cdp", workspace=ws, kind=Runner.EMDASH, status=Runner.ONLINE,
        last_heartbeat_at=now, owner=user, host="jj@mbp", capabilities=CAPS))
    other = enrolled(Runner.objects.create(
        name="hal-mbp-cdp", workspace=ws, kind=Runner.EMDASH, status=Runner.ONLINE,
        last_heartbeat_at=now, owner=user, host="hal@mbp", capabilities=CAPS))
    cloud = enrolled(Runner.objects.create(
        name="cloud-ec2-2", workspace=ws, kind=Runner.CLOUD, status=Runner.ONLINE,
        last_heartbeat_at=now, owner=user, host="cloud-ec2-2", capabilities=CAPS))
    c = Client()
    c.force_login(user)
    return SimpleNamespace(user=user, ws=ws, laptop=laptop, other=other, cloud=cloud, c=c)


def _session(ctx, runner, *, key="a-task", **binding):
    s = Session.objects.create(workspace=ctx.ws, project="canopy-web", title=key,
                               created_by=ctx.user)
    RunnerBinding.objects.create(session=s, runner=runner, session_key=key,
                                 emdash_project="canopy-web", host=runner.host,
                                 thread_key=str(s.id), live_seen_at=timezone.now(), **binding)
    return s


def _transcript_sourced(s):
    from apps.canopy_sessions import services

    s.metadata = {**(s.metadata or {}), services.TRANSCRIPT_SOURCED: True}
    s.save(update_fields=["metadata"])
    return s


def _turn(s, key, *, status=Turn.QUEUED, by=None, **kw):
    return Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_API, idempotency_key=key,
                               status=status, enqueued_by=by or s.created_by, **kw)


def _row(ctx, s, **params):
    r = ctx.c.get("/api/canopy-sessions/", {"state": "all", **params})
    assert r.status_code == 200, r.content
    return next(x for x in r.json() if x["id"] == str(s.id))


# --- 1. one answer to "is it running" ---------------------------------------

def test_a_cloud_session_mid_turn_reads_running_and_is_not_your_move(ctx):
    """The cloud runner reports no engine flag; its running turn is the evidence.
    The agent having spoken last mid-turn is not "waiting on you"."""
    s = _session(ctx, ctx.cloud, key="uuid-claude-session")
    Message.objects.create(session=s, turn_index=0, role=Message.USER, plaintext="go")
    Message.objects.create(session=s, turn_index=64, role=Message.ASSISTANT, plaintext="on it")
    t = _turn(s, "k1", status=Turn.RUNNING, claimed_by=ctx.cloud, claimed_at=timezone.now())

    row = _row(ctx, s, reply="true")
    assert row["running"] is True
    assert row["feed_status"] == ""

    Turn.objects.filter(pk=t.pk).update(status=Turn.DONE, finished_at=timezone.now())
    row = _row(ctx, s, reply="true")
    assert row["running"] is False
    assert row["feed_status"] == feed.WAITING


def test_a_turn_on_a_box_that_stopped_heartbeating_is_not_running(ctx):
    s = _session(ctx, ctx.cloud, key="uuid-2")
    _turn(s, "k1", status=Turn.RUNNING, claimed_by=ctx.cloud, claimed_at=timezone.now())
    Runner.objects.filter(pk=ctx.cloud.pk).update(last_heartbeat_at=timezone.now() - dt.timedelta(hours=1))
    s = Session.objects.select_related("runner_binding__runner").get(pk=s.pk)
    assert session_state.is_running(s) is False


def test_work_no_turn_owns_is_read_from_the_engine(ctx):
    """A person typing straight into emdash: no Turn, the engine says working."""
    s = _session(ctx, ctx.laptop, agent_status="working")
    s = Session.objects.select_related("runner_binding__runner").get(pk=s.pk)
    assert session_state.is_running(s) is True
    assert session_state.activity(s) == session_state.WORKING


def test_a_queued_follow_up_is_not_your_move_either(ctx):
    s = _session(ctx, ctx.cloud, key="uuid-3")
    Message.objects.create(session=s, turn_index=64, role=Message.ASSISTANT, plaintext="done")
    _turn(s, "k1", status=Turn.QUEUED)
    assert _row(ctx, s, reply="true")["feed_status"] == ""


def test_the_socket_snapshot_carries_the_resolved_activity(ctx):
    from apps.canopy_sessions import agui, serializers

    s = _session(ctx, ctx.cloud, key="uuid-4")
    _turn(s, "k1", status=Turn.RUNNING, claimed_by=ctx.cloud, claimed_at=timezone.now())
    s = Session.objects.select_related("runner_binding__runner").get(pk=s.pk)
    dto = serializers.session_state_dto(
        session=s, current_user_id=ctx.user.pk, participants=[], present_ids=[], draft=None,
        messages=[], activity=session_state.activity(s))
    assert dto["activity"] == "working"
    events = agui.project({"event": "session.state", "data": dto}, thread_id=str(s.id))
    snap = [e for e in events if e.type == "ACTIVITY_SNAPSHOT"]
    assert snap and snap[0].content == {"state": "working"}


def test_ran_on_its_own_is_one_rule_in_python_and_sql(ctx):
    """The feed's AUTO and the cloud retire leg ask the same predicate."""
    cases = []
    for i, (mode, origin, slack) in enumerate([
        ("auto", Turn.ORIGIN_CANOPY_SCHEDULER, False), ("auto", Turn.ORIGIN_CANOPY_WEB_CHAT, False),
        ("auto", Turn.ORIGIN_SLACK, True), ("auto", Turn.ORIGIN_SLACK, False),
        ("manual", Turn.ORIGIN_CANOPY_SCHEDULER, False),
    ]):
        s = _session(ctx, ctx.cloud, key=f"uuid-r{i}")
        if slack:
            s.metadata = {"slack_thread": {"channel": "C1", "ts": "1"}}
            s.save(update_fields=["metadata"])
        Turn.objects.create(chat_session=s, origin=origin, idempotency_key=f"r{i}",
                            status=Turn.DONE, turn_mode=mode, claimed_by=ctx.cloud,
                            enqueued_by=ctx.user)
        cases.append(s)
    sql = set(Session.objects.filter(staleness.ran_on_its_own_q()).values_list("pk", flat=True))
    for s in cases:
        s = Session.objects.get(pk=s.pk)
        assert feed.ran_on_its_own(s) == (s.pk in sql), s.title
    assert [feed.ran_on_its_own(Session.objects.get(pk=s.pk)) for s in cases] == [
        True, False, False, True, False]


# --- 2. stop is durable --------------------------------------------------------

def test_a_stop_is_recorded_and_drained_by_the_runner(ctx, monkeypatch):
    from apps.realtime import groups

    frames = []
    monkeypatch.setattr(groups, "publish", lambda g, m: frames.append(m))
    s = _session(ctx, ctx.laptop)
    s = Session.objects.select_related("runner_binding__runner").get(pk=s.pk)
    assert control.interrupt_session(s) == "sent"
    pending = RunnerBinding.objects.get(session=s).pending_interrupt
    assert frames[-1]["interrupt_id"] == pending["id"]

    r = ctx.c.get(f"/api/harness/runners/{ctx.laptop.id}/session-interrupts")
    assert r.json()["interrupts"] == [{"session_id": str(s.id), "session_key": "a-task",
                                       "project": "canopy-web", "interrupt_id": pending["id"]}]
    # Another box cannot retire it; a stale id does not either.
    r = ctx.c.post(f"/api/harness/runners/{ctx.laptop.id}/session-interrupt-result",
                   data={"session_id": str(s.id), "interrupt_id": "old", "outcome": "idle"},
                   content_type="application/json")
    assert r.json() == {"ok": False}
    r = ctx.c.post(f"/api/harness/runners/{ctx.laptop.id}/session-interrupt-result",
                   data={"session_id": str(s.id), "interrupt_id": pending["id"],
                         "outcome": "interrupted"}, content_type="application/json")
    assert r.json() == {"ok": True}
    assert RunnerBinding.objects.get(session=s).pending_interrupt is None


def test_a_stop_older_than_its_ttl_is_not_offered(ctx):
    s = _session(ctx, ctx.laptop)
    RunnerBinding.objects.filter(session=s).update(pending_interrupt={
        "id": str(uuid.uuid4()), "at": time.time() - control.INTERRUPT_TTL.total_seconds() - 5})
    r = ctx.c.get(f"/api/harness/runners/{ctx.laptop.id}/session-interrupts")
    assert r.json()["interrupts"] == []


# --- 3. a transfer runs first --------------------------------------------------

def test_a_follow_up_waits_for_the_transfer_turn(ctx):
    """The target must not run a follow-up before it has restored the work."""
    s = _session(ctx, ctx.laptop)
    binding, turn = session_placement.transfer_session(session=s, placement=str(ctx.other.id),
                                                       user=ctx.user)
    assert turn is None and binding.runner_id == ctx.other.id   # the source is packing
    follow = _turn(s, "follow-1")

    assert claim.claim_next_turn(ctx.other) is None
    st = ts.resolve(follow)
    assert st.state == ts.QUEUED_BEHIND and st.behind == "transfer"

    # The source answers; the transfer turn exists and goes first, older follow-up or not.
    h = s.handoffs.get()
    SessionHandoff.objects.filter(pk=h.pk).update(capture_status=SessionHandoff.UNREACHABLE)
    from apps.canopy_sessions import handoff
    h = handoff.settle(SessionHandoff.objects.get(pk=h.pk))
    assert h.turn is not None
    assert routing.blocking_turn(follow).pk == h.turn_id
    claimed = claim.claim_next_turn(ctx.other)
    assert claimed is not None and claimed.pk == h.turn_id
    # From here the session's own lane holds it, as for any running turn.
    assert claim.claim_next_turn(ctx.other) is None


def test_the_source_cannot_take_the_session_back_mid_transfer(ctx):
    s = _session(ctx, ctx.laptop)
    session_placement.transfer_session(session=s, placement=str(ctx.other.id), user=ctx.user)
    session_sync.record_session(None, str(s.id), runner=ctx.laptop, project="canopy-web",
                                workspace=ctx.ws, session_key="a-task")
    assert RunnerBinding.objects.get(session=s).runner_id == ctx.other.id


# --- 4. a send that never landed -----------------------------------------------

def test_a_send_that_failed_before_landing_is_listed_with_its_state(ctx):
    s = _transcript_sourced(_session(ctx, ctx.cloud, key="uuid-5"))
    _m, t = session_send.send_message(session=s, text="please", user=ctx.user, client_id="c1")
    Turn.objects.filter(pk=t.pk).update(status=Turn.FAILED, finished_at=timezone.now(),
                                        result_note="the box lost its login")
    [entry] = session_send.ended_sends(s)
    assert entry["state"] == Turn.FAILED and entry["text"] == "please"
    assert entry["detail"] == "the box lost its login" and entry["cancelled_by"] == ""


def test_a_done_send_whose_row_never_landed_is_listed_after_the_grace(ctx):
    s = _transcript_sourced(_session(ctx, ctx.cloud, key="uuid-6"))
    earlier = _turn(s, "landed", status=Turn.DONE, prompt="earlier", by=ctx.user,
                    initiator_user=ctx.user)
    Message.objects.create(session=s, turn_index=0, role=Message.USER, plaintext="earlier",
                           source_turn_id=earlier.pk)
    _m, t = session_send.send_message(session=s, text="lost one", user=ctx.user, client_id="c2")
    Turn.objects.filter(pk=t.pk).update(status=Turn.DONE, finished_at=timezone.now())
    assert session_send.ended_sends(s) == [], "still inside the landing grace"
    Turn.objects.filter(pk=t.pk).update(
        finished_at=timezone.now() - session_send.DONE_LANDING_GRACE - dt.timedelta(seconds=1))
    assert [e["state"] for e in session_send.ended_sends(s)] == [Turn.DONE]


# --- 5. archive ---------------------------------------------------------------

def test_archive_cancels_open_turns_and_survives_the_report(ctx):
    s = _session(ctx, ctx.laptop)
    queued = _turn(s, "q1")
    r = ctx.c.post(f"/api/canopy-sessions/{s.id}/archive")
    assert r.status_code == 200 and r.json()["status"] == "archived"
    queued.refresh_from_db()
    assert queued.status == Turn.CANCELLED

    task = SimpleNamespace(emdash_task="a-task", project="canopy-web", status="in_progress",
                           last_interacted_at=None, recent_messages=[])
    session_sync.replace_reported_sessions(ctx.laptop, ctx.ws, [task], complete=True)
    assert Session.objects.get(pk=s.pk).status == Session.ARCHIVED
    # Gone from a complete report, then back: that is a reopen, and revives it.
    session_sync.replace_reported_sessions(ctx.laptop, ctx.ws, [], complete=True)
    session_sync.replace_reported_sessions(ctx.laptop, ctx.ws, [task], complete=True)
    assert Session.objects.get(pk=s.pk).status == Session.ACTIVE


def test_a_send_into_an_archived_session_brings_it_back(ctx):
    s = _session(ctx, ctx.cloud, key="uuid-7")
    control.archive_session(s)
    assert Session.objects.get(pk=s.pk).status == Session.ARCHIVED
    _m, turn = session_send.send_message(session=s, text="again", user=ctx.user, client_id="c3")
    s.refresh_from_db()
    assert s.status == Session.ACTIVE and Session.HAND_ARCHIVED_KEY not in (s.metadata or {})
    assert turn is not None
