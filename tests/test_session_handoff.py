"""A transfer carries the WORK, not just the conversation (`canopy_sessions/handoff.py`).

The motivating day (2026-10-23): an account out of tokens with 9 open sessions,
moved by reading another macOS user's disk — which works only on one laptop, gave
the new session a cold brief, and left 3 of 11 ghost tasks behind. Here the
source runner packs its own worktree (it still has a working runner when its
Claude account is out of tokens), the target restores it before its agent acts,
and the source's task is closed only once the work is safely elsewhere.

The packing is real git in runner/canopy_runner/tests/test_handoff.py; this is
the server's flow, driven through the runner protocol the way a box drives it.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions import handoff
from apps.canopy_sessions import placement as session_placement
from apps.canopy_sessions.models import HandoffArchive, Message, RunnerBinding, Session, SessionHandoff
from apps.harness.models import Runner, Turn
from apps.harness.session_sync import replace_reported_sessions
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

CAPS = {"sessions": True, "projects": ["canopy-web"]}
ARCHIVE = b"\x1f\x8b pretend tar.gz " * 1000


def _manifest(**over):
    m = {"format": 1, "repo": "github.com/dimagi-internal/canopy-web", "branch": "feature/x",
         "head": "a" * 40, "upstream": "", "local_commits": 2,
         "staged_files": ["apps/a.py"], "unstaged_files": ["apps/b.py"],
         "untracked_files": ["notes.md"], "skipped": [{"path": ".env.local",
                                                      "why": "its name says it holds a secret"}],
         "status": ["A  apps/a.py", " M apps/b.py", "?? notes.md"], "diff_sha256": "d",
         "transcript": None, "cwd": "/Users/hal/emdash/worktrees/x",
         "bytes": len(ARCHIVE), "sha256": hashlib.sha256(ARCHIVE).hexdigest()}
    m.update(over)
    return m


@pytest.fixture
def ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    now = timezone.now()
    source = Runner.objects.create(name="haldimagi-mbp-cdp", workspace=ws, kind=Runner.EMDASH,
                                   status=Runner.ONLINE, last_heartbeat_at=now, owner=user,
                                   host="hal@mbp", capabilities=CAPS)
    laptop = Runner.objects.create(name="jj-mbp-cdp", workspace=ws, kind=Runner.EMDASH,
                                   status=Runner.ONLINE, last_heartbeat_at=now, owner=user,
                                   host="jj@mbp", capabilities=CAPS)
    cloud = Runner.objects.create(name="cloud-ec2-1", workspace=ws, kind=Runner.CLOUD,
                                  status=Runner.ONLINE, last_heartbeat_at=now, owner=user,
                                  host="cloud-ec2-1", capabilities=CAPS)
    c = Client()
    c.force_login(user)
    return SimpleNamespace(user=user, ws=ws, source=source, laptop=laptop, cloud=cloud, c=c)


def _session(ctx, task="export-audit", runner=None):
    s = Session.objects.create(workspace=ctx.ws, project="canopy-web", title=task,
                               created_by=ctx.user)
    RunnerBinding.objects.create(session=s, runner=runner or ctx.source, session_key=task,
                                 emdash_project="canopy-web", host="hal@mbp",
                                 transcript_id="src-claude-uuid", thread_key=str(s.id))
    for i in (0, 64, 128):
        Message.objects.create(session=s, turn_index=i, role=Message.ASSISTANT, plaintext=f"r{i}")
    return s


def _upload(ctx, h, archive=ARCHIVE, manifest=None):
    half = len(archive) // 2
    for i, chunk in enumerate((archive[:half], archive[half:])):
        r = ctx.c.post(f"/api/harness/runners/{ctx.source.id}/handoffs/{h.id}/archive",
                       data={"part": i, "parts": 2, "data": base64.b64encode(chunk).decode()},
                       content_type="application/json")
        assert r.status_code == 200, r.content
    r = ctx.c.post(f"/api/harness/runners/{ctx.source.id}/handoffs/{h.id}/captured",
                   data={"ok": True, "manifest": manifest or _manifest()},
                   content_type="application/json")
    assert r.status_code == 200, r.content
    h.refresh_from_db()
    return h


def _due(ctx, runner=None):
    r = ctx.c.get(f"/api/harness/runners/{(runner or ctx.source).id}/handoffs")
    assert r.status_code == 200, r.content
    return r.json()["handoffs"]


def _report(task, project="canopy-web"):
    return SimpleNamespace(emdash_task=task, project=project, status="in_progress",
                           last_interacted_at=None, recent_messages=[])


# --- the whole loop -------------------------------------------------------

def test_a_reachable_source_packs_the_target_restores_then_the_source_closes(ctx):
    s = _session(ctx)
    binding, turn = session_placement.transfer_session(session=s, placement=str(ctx.laptop.id),
                                                       brief="Ship the export audit.",
                                                       user=ctx.user)
    # Moved now, but the turn waits for the work.
    assert binding.runner_id == ctx.laptop.id and turn is None
    h = s.handoffs.get()
    assert h.capture_status == SessionHandoff.PENDING and h.source_session_key == "export-audit"
    (due,) = _due(ctx)
    assert due["capture"] and not due["close"] and due["session_key"] == "export-audit"

    # Until the source closes it, its report must not file the task as a NEW session.
    replace_reported_sessions(ctx.source, ctx.ws, [_report("export-audit")])
    assert Session.objects.filter(title="export-audit").count() == 1

    h = _upload(ctx, h)
    assert h.capture_status == SessionHandoff.CAPTURED and h.turn is not None
    turn = h.turn
    assert turn.pinned_runner_id == ctx.laptop.id
    assert "The work came with you" in turn.prompt
    assert "`feature/x`" in turn.prompt and "2 local commit(s)" in turn.prompt
    assert "`apps/a.py`" in turn.prompt and "`notes.md`" in turn.prompt
    assert "`.env.local`" in turn.prompt and "canopy-handoff.json" in turn.prompt
    assert "Ship the export audit." in turn.prompt and "NO SNAPSHOT" not in turn.prompt
    assert turn.origin_ref["handoff"] == {"id": str(h.id), "resume": False}
    # A laptop target opens a fresh Claude session: a new epoch above the history.
    binding.refresh_from_db()
    assert binding.index_offset > 128 and binding.transcript_id == ""
    assert _due(ctx) == [], "nothing to close until the target has the work"

    r = ctx.c.get(f"/api/harness/runners/{ctx.laptop.id}/handoffs/{h.id}/archive")
    assert r.status_code == 200 and r.content == ARCHIVE
    r = ctx.c.post(f"/api/harness/runners/{ctx.laptop.id}/handoffs/{h.id}/restored",
                   data={"report": {"ok": True, "restored": True, "mismatch": []}},
                   content_type="application/json")
    assert r.json()["restore_status"] == "restored"

    (due,) = _due(ctx)
    assert due["close"] and not due["capture"]
    r = ctx.c.post(f"/api/harness/runners/{ctx.source.id}/handoffs/{h.id}/close-result",
                   data={"ok": True}, content_type="application/json")
    assert r.json()["close_status"] == "closed"
    assert _due(ctx) == []


def test_only_the_boxes_in_the_transfer_reach_its_snapshot(ctx):
    s = _session(ctx)
    session_placement.transfer_session(session=s, placement=str(ctx.laptop.id), user=ctx.user)
    h = _upload(ctx, s.handoffs.get())
    r = ctx.c.get(f"/api/harness/runners/{ctx.cloud.id}/handoffs/{h.id}/archive")
    assert r.status_code == 404
    r = ctx.c.post(f"/api/harness/runners/{ctx.laptop.id}/handoffs/{h.id}/captured",
                   data={"ok": False, "error": "x"}, content_type="application/json")
    assert r.status_code == 404, "only the source answers for the source"


def test_a_paused_source_still_packs(ctx):
    """A box parked because its account is out of tokens is the motivating case."""
    ctx.source.status = Runner.PAUSED
    ctx.source.save(update_fields=["status"])
    assert ctx.source.is_reachable
    s = _session(ctx)
    session_placement.transfer_session(session=s, placement=str(ctx.laptop.id), user=ctx.user)
    assert s.handoffs.get().capture_status == SessionHandoff.PENDING


def test_an_offline_source_transfers_with_no_snapshot_and_packs_when_back(ctx):
    ctx.source.last_heartbeat_at = timezone.now() - dt.timedelta(hours=2)
    ctx.source.save(update_fields=["last_heartbeat_at"])
    s = _session(ctx)
    _b, turn = session_placement.transfer_session(session=s, placement=str(ctx.laptop.id),
                                                  user=ctx.user)
    assert turn is not None, "a source that cannot answer never holds the session hostage"
    assert "NO SNAPSHOT" in turn.prompt and "haldimagi-mbp-cdp is stale" in turn.prompt
    assert "NOT a record of the files" in turn.prompt
    assert "handoff" not in turn.origin_ref
    h = s.handoffs.get()
    assert h.capture_status == SessionHandoff.UNREACHABLE and h.close_status == "pending"

    # It comes back: it packs late, the snapshot is held, and then it may close.
    (due,) = _due(ctx)
    assert due["capture"]
    h = _upload(ctx, h)
    assert h.capture_status == SessionHandoff.CAPTURED and "after the transfer" in h.capture_note
    assert h.turn_id == turn.id, "the transfer already went; no second turn"
    (due,) = _due(ctx)
    assert due["close"]
    # ...and the person can put that late snapshot into the session on demand.
    turn.status = Turn.DONE
    turn.save(update_fields=["status"])
    r = ctx.c.post(f"/api/canopy-sessions/{s.id}/handoffs/{h.id}/restore")
    assert r.status_code == 200, r.content
    late = Turn.objects.filter(chat_session=s).exclude(pk=turn.pk).get()
    assert late.origin_ref["handoff"]["id"] == str(h.id) and late.pinned_runner_id == ctx.laptop.id


def test_a_source_that_never_answers_is_settled_by_the_clock(ctx):
    s = _session(ctx)
    session_placement.transfer_session(session=s, placement=str(ctx.laptop.id), user=ctx.user)
    assert handoff.settle_overdue() == 0
    assert handoff.settle_overdue(now=timezone.now() + handoff.CAPTURE_WAIT
                                  + dt.timedelta(seconds=1)) == 1
    h = s.handoffs.get()
    assert h.capture_status == SessionHandoff.TIMED_OUT
    assert "NO SNAPSHOT" in h.turn.prompt and "within 3 minutes" in h.turn.prompt


def test_work_that_cannot_be_packed_keeps_the_source_task_open(ctx):
    s = _session(ctx)
    session_placement.transfer_session(session=s, placement=str(ctx.laptop.id), user=ctx.user)
    h = s.handoffs.get()
    r = ctx.c.post(f"/api/harness/runners/{ctx.source.id}/handoffs/{h.id}/captured",
                   data={"ok": False, "error": "the work is 31.0 MB packed, over the 24.0 MB cap"},
                   content_type="application/json")
    assert r.status_code == 200
    h.refresh_from_db()
    assert h.capture_status == SessionHandoff.FAILED and h.close_status == SessionHandoff.KEPT
    assert "NO SNAPSHOT" in h.turn.prompt and "31.0 MB" in h.turn.prompt
    assert _due(ctx) == [], "a task holding the only copy is never closed unasked"


def test_a_snapshot_that_does_not_match_its_checksum_is_refused(ctx):
    s = _session(ctx)
    session_placement.transfer_session(session=s, placement=str(ctx.laptop.id), user=ctx.user)
    h = _upload(ctx, s.handoffs.get(), manifest=_manifest(sha256="0" * 64))
    assert h.capture_status == SessionHandoff.FAILED and "checksum" in h.capture_note
    assert not HandoffArchive.objects.filter(handoff=h).exists()


def test_a_failed_restore_keeps_the_source_open_and_raises_an_alert(ctx):
    from apps.canopy_sessions import alerts_handoff

    s = _session(ctx)
    session_placement.transfer_session(session=s, placement=str(ctx.laptop.id), user=ctx.user)
    h = _upload(ctx, s.handoffs.get())
    ctx.c.post(f"/api/harness/runners/{ctx.laptop.id}/handoffs/{h.id}/restored",
               data={"report": {"ok": False, "restored": True,
                                "mismatch": [{"what": "git status"}]}},
               content_type="application/json")
    h.refresh_from_db()
    assert h.restore_status == SessionHandoff.MISMATCH and h.close_status == SessionHandoff.KEPT
    assert _due(ctx) == []
    (raised,) = alerts_handoff.reconcile(timezone.now())
    assert raised.title == '"export-audit" was left open on haldimagi-mbp-cdp after its transfer'
    alert = SimpleNamespace(subject_key=raised.subject_key)
    assert [a.id for a in alerts_handoff.actions(alert, ctx.user)] == ["close", "dismiss"]
    alerts_handoff.perform(alert, ctx.user, "close")
    (due,) = _due(ctx)
    assert due["close"], "a person said close it anyway"


def test_a_close_that_keeps_failing_is_an_alert_not_a_silent_column(ctx):
    from apps.canopy_sessions import alerts_handoff

    s = _session(ctx)
    session_placement.transfer_session(session=s, placement=str(ctx.laptop.id), user=ctx.user)
    h = _upload(ctx, s.handoffs.get())
    handoff.record_restore(h, {"ok": True, "restored": True})
    ctx.c.post(f"/api/harness/runners/{ctx.source.id}/handoffs/{h.id}/close-result",
               data={"ok": False, "error": 'emdash: task "export-audit" is still in the sidebar '
                                           "after the delete"},
               content_type="application/json")
    (raised,) = alerts_handoff.reconcile(timezone.now())
    assert "keeps failing" in raised.title and "still in the sidebar" in raised.body
    (due,) = _due(ctx)
    assert due["close"] and "still in the sidebar" in due["close_error"], "it keeps retrying"


def test_a_cloud_target_resumes_the_conversation(ctx):
    s = _session(ctx)
    binding, _ = session_placement.transfer_session(session=s, placement=str(ctx.cloud.id),
                                                    user=ctx.user)
    h = s.handoffs.get()
    assert h.want_transcript
    (due,) = _due(ctx)
    assert due["transcript"]
    h = _upload(ctx, h, manifest=_manifest(transcript={"session_id": "src-claude-uuid",
                                                       "records": 40}))
    assert h.resume_planned
    binding.refresh_from_db()
    # Same Claude session continues: same ordinals, same transcript id, no new epoch.
    assert binding.index_offset == 0 and binding.transcript_id == "src-claude-uuid"
    ref = h.turn.origin_ref["handoff"]
    assert ref["resume"] is True and "FRESH session" in ref["cold_prompt"]
    assert "you have it in context" in h.turn.prompt
    assert "/Users/hal/emdash/worktrees/x" in h.turn.prompt


def test_a_sensitive_conversations_work_is_never_packed(ctx, monkeypatch):
    from apps.harness import sensitivity

    monkeypatch.setattr(sensitivity, "is_sensitive", lambda obj: True)
    s = _session(ctx)
    _b, turn = session_placement.transfer_session(session=s, placement=str(ctx.laptop.id),
                                                  user=ctx.user)
    h = s.handoffs.get()
    assert h.capture_status == SessionHandoff.NOT_ASKED and h.close_status == SessionHandoff.KEPT
    assert "NO SNAPSHOT" in turn.prompt and "sensitive conversation" in turn.prompt
    assert _due(ctx) == []


# --- discovery and batch ---------------------------------------------------

def test_list_sessions_on_a_runner(ctx):
    a, b = _session(ctx, "one"), _session(ctx, "two")
    _session(ctx, "elsewhere", runner=ctx.laptop)
    r = ctx.c.get("/api/canopy-sessions/", {"runner": "haldimagi-mbp-cdp"})
    assert {x["id"] for x in r.json()} == {str(a.id), str(b.id)}


def test_transfer_all_moves_each_session_and_says_what_happened(ctx):
    a, b, busy = _session(ctx, "one"), _session(ctx, "two"), _session(ctx, "busy")
    Turn.objects.create(chat_session=busy, workspace=ctx.ws, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                        idempotency_key="busy", status=Turn.RUNNING, claimed_by=ctx.source)
    body = {"from_runner": "haldimagi-mbp-cdp", "to_runner": "jj-mbp-cdp", "dry_run": True}
    r = ctx.c.post("/api/canopy-sessions/transfer-all", data=body, content_type="application/json")
    assert r.status_code == 200, r.content
    assert {x["session_key"]: x["status"] for x in r.json()["results"]} == {
        "one": "would_move", "two": "would_move", "busy": "refused"}
    assert not SessionHandoff.objects.exists(), "a dry run moves nothing"

    body["dry_run"] = False
    body["brief"] = "Account switch."
    out = ctx.c.post("/api/canopy-sessions/transfer-all", data=body,
                     content_type="application/json").json()
    by = {x["session_key"]: x for x in out["results"]}
    assert by["one"]["status"] == by["two"]["status"] == "moved"
    assert by["one"]["handoff"]["capture_status"] == "pending"
    assert by["busy"]["status"] == "refused" and "executing" in by["busy"]["error"]
    for s in (a, b):
        assert RunnerBinding.objects.get(session=s).runner_id == ctx.laptop.id
    assert {d["session_key"] for d in _due(ctx)} == {"one", "two"}


def test_handoffs_route_reports_each_transfer(ctx):
    s = _session(ctx)
    session_placement.transfer_session(session=s, placement=str(ctx.laptop.id), user=ctx.user)
    _upload(ctx, s.handoffs.get())
    (row,) = ctx.c.get(f"/api/canopy-sessions/{s.id}/handoffs").json()
    assert row["capture_status"] == "captured" and row["branch"] == "feature/x"
    assert row["skipped"][0]["path"] == ".env.local" and row["turn_id"]


# --- a session working right now is not moved ------------------------------

def test_a_session_the_engine_says_is_working_is_not_moved(ctx):
    """Typed straight into (the chat, /ide, emdash), a session runs Claude with no
    executing Turn at all; its runner's report is the only witness. Found live
    2026-10-24: a dry run offered three `running` sessions, one of them the very
    session asking."""
    from apps.canopy_sessions import transcript_read

    s = _session(ctx, "live")
    RunnerBinding.objects.filter(session=s).update(agent_status="working")
    s.refresh_from_db()
    assert transcript_read.is_session_running(s.runner_binding)
    assert not Turn.objects.filter(chat_session=s).exists()

    with pytest.raises(RuntimeError, match="working right now"):
        session_placement.transfer_session(session=s, placement=str(ctx.laptop.id), user=ctx.user)
    body = {"from_runner": "haldimagi-mbp-cdp", "to_runner": "jj-mbp-cdp"}
    for dry in (True, False):
        (row,) = ctx.c.post("/api/canopy-sessions/transfer-all", data={**body, "dry_run": dry},
                            content_type="application/json").json()["results"]
        assert row["status"] == "refused" and "working right now" in row["error"]
    assert RunnerBinding.objects.get(session=s).runner_id == ctx.source.id


def test_a_paused_box_mid_turn_is_still_busy_and_an_offline_one_is_not(ctx):
    s = _session(ctx, "live")
    RunnerBinding.objects.filter(session=s).update(agent_status="working")
    ctx.source.status = Runner.PAUSED  # a pause stops new work, not a turn under way
    ctx.source.save(update_fields=["status"])
    s.refresh_from_db()
    assert session_placement.busy_reason(s) == "the session is working right now — stop it first"
    # Offline: its last report describes a session nobody can still be driving.
    Runner.objects.filter(pk=ctx.source.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=2))
    s = Session.objects.get(pk=s.pk)
    assert session_placement.busy_reason(s) == ""
    session_placement.transfer_session(session=s, placement=str(ctx.laptop.id), user=ctx.user)
