"""How full a session's context window is, end to end.

The runner reads it off each assistant record (`canopy_transcript.record_window`:
input + both cache counts) and ships it on the row as `content.usage`; the row is
stored with it and the live frame carries it, so the chat's meter reads the
newest row that has one. The window's SIZE comes from the box: the transcript
records the bare API model id and never the `[1m]` that picks the 1M window, so
the runner reports its configured model (`claude_model`) on the heartbeat and the
session payload serves it as `runner_model`.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.canopy_sessions import session_row
from apps.canopy_sessions import services as chat
from apps.canopy_sessions import transcript_ingest
from apps.canopy_sessions.models import Message, RunnerBinding
from apps.canopy_sessions.stream_map import turn_event_to_frames
from apps.harness import services as harness
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

USAGE = {"context_tokens": 124_005, "model": "claude-opus-5-5"}


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(
        name="jj-mbp-cdp", kind=Runner.EMDASH, host="h", owner=user, workspace_id=ws.pk,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={"sessions": True})
    session = chat.create_session(workspace=ws, created_by=user)
    return user, session, runner


def test_the_live_frame_carries_the_calls_usage():
    frames = turn_event_to_frames(
        {"seq": 9, "kind": "assistant", "ts": "t", "payload": {"text": "done", "usage": USAGE}},
        lambda seq: f"m:{seq}")
    assert frames[1]["data"]["usage"] == USAGE
    plain = turn_event_to_frames({"seq": 1, "kind": "assistant", "ts": "t", "payload": {"text": "x"}},
                                 lambda seq: f"m:{seq}")
    assert "usage" not in plain[1]["data"]


def test_a_shipped_row_is_stored_with_its_usage():
    _user, session, _runner = _ctx()
    transcript_ingest.persist_transcript_rows(session, [
        {"index": 10, "role": "assistant", "text": "done", "content": {"usage": USAGE}}])
    assert Message.objects.get(session=session, turn_index=10).content["usage"] == USAGE


def test_the_runner_reports_its_model_and_the_session_serves_it():
    _user, session, runner = _ctx()
    harness.heartbeat(runner, active_turn_ids=[], claude_model="opus[1m]")
    RunnerBinding.objects.create(session=session, runner=runner, thread_key="tk")
    runner.refresh_from_db()
    assert runner.capabilities["claude_model"] == "opus[1m]"
    assert session_row.session_out(session)["runner_model"] == "opus[1m]"
    # Absent (older code) clears it, like every other reported capability.
    harness.heartbeat(runner, active_turn_ids=[])
    runner.refresh_from_db()
    assert runner.capabilities["claude_model"] == ""


def test_an_unbound_session_has_no_runner_model():
    _user, session, _runner = _ctx()
    assert session_row.session_out(session)["runner_model"] == ""


def test_claude_model_is_reported_not_set_by_hand(client):
    user, _session, runner = _ctx()
    client.force_login(user)
    resp = client.patch(f"/api/harness/runners/{runner.id}", data={"capabilities": {"claude_model": "x"}},
                        content_type="application/json")
    assert resp.status_code == 422
