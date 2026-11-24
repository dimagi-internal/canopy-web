"""A runner's sync version and `GET /runners/{id}/sync`.

* Every write that records work a box owes moves its version, in the write's own
  transaction (`runner_sync.bump`, an F() increment).
* `/sync` answers 304 to its own ETag having read only the runner row, and a 200
  with every pending row otherwise — so a doorbell that never arrived is caught
  by the next read.
* `Runner.save` never writes the version back from a stale instance.
* `v` rides the heartbeat reply and the doorbell frames.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.canopy_sessions import control, transcript_read
from apps.canopy_sessions import placement as session_placement
from apps.canopy_sessions.models import RunnerBinding, Session, SessionHandoff
from apps.harness import runner_projects, runner_sync
from apps.harness import runners as harness_runners
from apps.harness import services as harness_services
from apps.harness.models import Runner, RunnerMint, Turn
from apps.harness.testing import enrolled
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

CAPS = {"sessions": True, "projects": ["canopy-web"]}


@pytest.fixture
def ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    now = timezone.now()
    laptop = enrolled(Runner.objects.create(
        name="jj-mbp", workspace=ws, kind=Runner.EMDASH, status=Runner.ONLINE,
        last_heartbeat_at=now, owner=user, host="jj@mbp", capabilities=CAPS))
    other = enrolled(Runner.objects.create(
        name="hal-mbp", workspace=ws, kind=Runner.EMDASH, status=Runner.ONLINE,
        last_heartbeat_at=now, owner=user, host="hal@mbp", capabilities=CAPS))
    c = Client()
    c.force_login(user)
    return SimpleNamespace(user=user, ws=ws, laptop=laptop, other=other, c=c)


def _session(ctx, runner, *, key="a-task"):
    s = Session.objects.create(workspace=ctx.ws, project="canopy-web", title=key,
                               created_by=ctx.user)
    RunnerBinding.objects.create(session=s, runner=runner, session_key=key,
                                 emdash_project="canopy-web", host=runner.host,
                                 thread_key=str(s.id), live_seen_at=timezone.now(),
                                 reported_at=timezone.now())
    return Session.objects.select_related("runner_binding__runner").get(pk=s.pk)


def _v(runner) -> int:
    return runner_sync.version_of(runner)


def _sync(ctx, runner, etag=None):
    headers = {"HTTP_IF_NONE_MATCH": etag} if etag else {}
    return ctx.c.get(f"/api/harness/runners/{runner.id}/sync", **headers)


# --- every work write moves the version ------------------------------------------

def test_a_stop_moves_the_version(ctx):
    s = _session(ctx, ctx.laptop)
    before = _v(ctx.laptop)
    assert control.interrupt_session(s) == "sent"
    assert _v(ctx.laptop) == before + 1


def test_a_menu_answer_moves_the_version(ctx):
    s = _session(ctx, ctx.laptop)
    before = _v(ctx.laptop)
    assert control.answer_menu(session=s, option=1) == "sent"
    assert _v(ctx.laptop) == before + 1


def test_a_close_moves_the_version(ctx):
    s = _session(ctx, ctx.laptop)
    before = _v(ctx.laptop)
    assert control.close_session(session=s) == "closing"
    assert _v(ctx.laptop) == before + 1


def test_a_backfill_ask_moves_the_version(ctx):
    s = _session(ctx, ctx.laptop)
    before = _v(ctx.laptop)
    assert transcript_read.request_backfill(s) == "requested"
    assert _v(ctx.laptop) == before + 1


def test_a_transfer_moves_both_boxes(ctx):
    s = _session(ctx, ctx.laptop)
    src, dst = _v(ctx.laptop), _v(ctx.other)
    session_placement.transfer_session(session=s, placement=str(ctx.other.id), user=ctx.user)
    assert _v(ctx.laptop) > src and _v(ctx.other) > dst


def test_a_sign_in_moves_the_version_and_rings(ctx, monkeypatch):
    frames = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda group, msg: frames.append((group, msg)))
    before = _v(ctx.laptop)
    mint = harness_runners.start_runner_mint(ctx.laptop, requested_by=ctx.user)
    assert _v(ctx.laptop) == before + 1
    harness_runners.submit_mint_code(mint, "abc")
    assert _v(ctx.laptop) == before + 2


def test_an_add_project_ask_moves_the_version(ctx):
    before = _v(ctx.laptop)
    runner_projects.request_project(ctx.laptop, repo_url="https://github.com/x/new-repo.git",
                                    user=ctx.user)
    assert _v(ctx.laptop) == before + 1


def test_a_stop_of_a_turn_moves_its_runners_version(ctx):
    s = _session(ctx, ctx.laptop)
    turn = Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_API, idempotency_key="t1",
                               status=Turn.RUNNING, enqueued_by=ctx.user, claimed_by=ctx.laptop)
    before = _v(ctx.laptop)
    harness_services.cancel_turn(turn, by="jj")
    assert _v(ctx.laptop) == before + 1


def test_a_bump_is_an_increment_in_the_database_not_a_written_value(ctx):
    """Two writers holding the same old row each move it: the UPDATE is
    `sync_version = sync_version + 1`, never a value computed in Python."""
    with CaptureQueriesContext(connection) as q:
        runner_sync.bump(ctx.laptop.id)
    assert '"sync_version" = ("harness_runner"."sync_version" + 1)' in q.captured_queries[-1]["sql"]
    runner_sync.bump(ctx.laptop, ctx.other)
    runner_sync.bump(ctx.laptop.id, ctx.laptop.id)  # one bump per runner named
    assert (_v(ctx.laptop), _v(ctx.other)) == (3, 1)


def test_saving_a_stale_runner_never_writes_the_version_back(ctx):
    stale = Runner.objects.get(pk=ctx.laptop.pk)
    runner_sync.bump(ctx.laptop)
    stale.status_note = "x"
    stale.save()
    stale.save(update_fields=["status_note", "sync_version"])
    assert _v(ctx.laptop) == 1


# --- GET /sync ---------------------------------------------------------------------

def test_an_unchanged_version_answers_304_reading_only_the_runner(ctx):
    r = _sync(ctx, ctx.laptop)
    assert r.status_code == 200
    tag = r["ETag"]
    assert tag == runner_sync.etag(_v(ctx.laptop))
    with CaptureQueriesContext(connection) as q:
        r = _sync(ctx, ctx.laptop, etag=tag)
    assert r.status_code == 304 and r["ETag"] == tag
    runner_reads = [x["sql"] for x in q.captured_queries if '"harness_runner"' in x["sql"]]
    # Signing in, plus the one read of the runner row the version comes from.
    assert len(runner_reads) == 1, runner_reads
    assert not any("canopy_sessions" in x["sql"] for x in q.captured_queries)


def test_a_dropped_frame_is_caught_by_the_reconcile(ctx, monkeypatch):
    """The stop's frame never reaches the box; its next `/sync` with the old ETag
    still names the stop, because the write moved the version."""
    monkeypatch.setattr("apps.realtime.groups.publish", lambda *a, **k: None)  # the frame is lost
    s = _session(ctx, ctx.laptop)
    tag = _sync(ctx, ctx.laptop)["ETag"]
    assert control.interrupt_session(s) == "sent"
    r = _sync(ctx, ctx.laptop, etag=tag)
    assert r.status_code == 200 and r["ETag"] != tag
    pending = RunnerBinding.objects.get(session=s).pending_interrupt
    assert [i["interrupt_id"] for i in r.json()["interrupts"]] == [pending["id"]]
    # ...and with the new ETag, nothing new: 304.
    assert _sync(ctx, ctx.laptop, etag=r["ETag"]).status_code == 304


def test_sync_answers_what_each_drain_answers(ctx):
    s = _session(ctx, ctx.laptop)
    control.interrupt_session(s)
    control.answer_menu(session=s, option=2)
    t = _session(ctx, ctx.laptop, key="b-task")
    transcript_read.request_backfill(t)
    u = _session(ctx, ctx.laptop, key="c-task")
    control.close_session(session=u)
    body = _sync(ctx, ctx.laptop).json()
    base = f"/api/harness/runners/{ctx.laptop.id}"
    assert body["interrupts"] == ctx.c.get(f"{base}/session-interrupts").json()["interrupts"]
    assert body["answers"] == ctx.c.get(f"{base}/menu-answers").json()["answers"]
    assert body["closes"] == ctx.c.get(f"{base}/closes").json()["closes"]
    assert body["backfills"] == ctx.c.get(f"{base}/backfills").json()["backfills"]
    assert body["handoffs"] == ctx.c.get(f"{base}/handoffs").json()["handoffs"]
    assert body["streams"] == ctx.c.get(f"{base}/streams").json()["streams"]
    assert body["version"] == _v(ctx.laptop)
    assert body["mint_due"] is False
    assert "listen" in body["sections"]


def test_sync_says_a_sign_in_is_due_without_consuming_it(ctx):
    mint = harness_runners.start_runner_mint(ctx.laptop, requested_by=ctx.user)
    assert _sync(ctx, ctx.laptop).json()["mint_due"] is True
    harness_runners.submit_mint_code(mint, "code-1")
    assert _sync(ctx, ctx.laptop).json()["mint_due"] is True
    assert RunnerMint.objects.get(pk=mint.pk).code == "code-1"  # still the claim's to take


def test_a_transfer_names_the_pack_in_the_sources_sync(ctx):
    s = _session(ctx, ctx.laptop)
    session_placement.transfer_session(session=s, placement=str(ctx.other.id), user=ctx.user)
    h = SessionHandoff.objects.get(session=s)
    assert [x["id"] for x in _sync(ctx, ctx.laptop).json()["handoffs"]] == [str(h.id)]


def test_sync_carries_a_stop_of_a_running_turn(ctx):
    s = _session(ctx, ctx.laptop)
    turn = Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_API, idempotency_key="t2",
                               status=Turn.RUNNING, enqueued_by=ctx.user, claimed_by=ctx.laptop)
    harness_services.cancel_turn(turn, by="jj")
    assert _sync(ctx, ctx.laptop).json()["cancel_turn_ids"] == [str(turn.id)]


# --- `v` on the heartbeat, and the `sync` capability -------------------------------

def test_the_heartbeat_reply_carries_v_and_records_sync(ctx):
    runner_sync.bump(ctx.laptop)
    r = ctx.c.post(f"/api/harness/runners/{ctx.laptop.id}/heartbeat",
                   data={"active_turn_ids": [], "sync": 1}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json()["sync_version"] == _v(ctx.laptop) == 1
    assert Runner.objects.get(pk=ctx.laptop.pk).capabilities["sync"] == 1
    # An older box (no flag) is recorded as 0: the drain routes are still in use.
    ctx.c.post(f"/api/harness/runners/{ctx.laptop.id}/heartbeat",
               data={"active_turn_ids": []}, content_type="application/json")
    assert Runner.objects.get(pk=ctx.laptop.pk).capabilities["sync"] == 0
