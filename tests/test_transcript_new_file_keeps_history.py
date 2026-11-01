"""A conversation that continues in a NEW transcript file keeps what came before.

A failover or `/clear` restarts the transcript's ordinals at 0 under a new id, and
the runner records the session again under a new key for the same thread. The
earlier file's rows stay; the new file's land in a new epoch above them, in order,
once each — live and after a reload.

Live is the half that can go wrong: a frame named by the raw ordinal gives the new
file's record 0 the same id and turn_index as the old file's record 0, and the
client upserts on those, so each new row overwrote an old one on screen. A frame
names the stored row instead, so the two files' rows never share an identity.
"""
from __future__ import annotations

import pytest
from canopy_transcript import BLOCK_STRIDE
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import services as chat_services
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="D", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=ws, owner=owner)
    box = Runner.objects.create(name="laptop", kind=Runner.EMDASH, host="mbp", owner=owner,
                                workspace=ws, status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    session = Session.objects.create(workspace=ws, agent=ada, title="chat", created_by=owner,
                                     metadata={chat_services.TRANSCRIPT_SOURCED: True})
    c = Client()
    c.force_login(owner)
    return {"session": session, "box": box, "client": c}


def _record(w, session_key):
    r = w["client"].post(f"/api/harness/runners/{w['box'].pk}/record-session",
                         {"thread_key": str(w["session"].pk), "session_key": session_key,
                          "session_id": session_key, "agent_slug": "ada"},
                         content_type="application/json")
    assert r.status_code == 200, r.content


def _ship(w, transcript_id, events):
    r = w["client"].post(f"/api/harness/runners/{w['box'].pk}/session-stream",
                         {"session_id": str(w["session"].pk), "transcript_id": transcript_id,
                          "events": events}, content_type="application/json")
    assert r.status_code == 200, r.content


def _turn(ask, reply):
    return [
        {"kind": "user", "seq": 0, "index": 0, "payload": {"text": ask}},
        {"kind": "tool_use", "seq": BLOCK_STRIDE, "index": BLOCK_STRIDE,
         "payload": {"id": f"tu-{ask}", "name": "Bash", "input": {}, "text": ""}},
        {"kind": "tool_result", "seq": 2 * BLOCK_STRIDE, "index": 2 * BLOCK_STRIDE,
         "payload": {"tool_use_id": f"tu-{ask}", "text": "ok"}},
        {"kind": "assistant", "seq": 3 * BLOCK_STRIDE, "index": 3 * BLOCK_STRIDE,
         "payload": {"text": reply}},
    ]


def test_a_new_transcript_file_lands_above_the_earlier_one(world, monkeypatch):
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append(m))
    _record(world, "T1")
    RunnerBinding.objects.filter(session=world["session"]).update(stream_desired=True)
    _ship(world, "T1", _turn("first ask", "echo: first ask"))
    _record(world, "T2")
    _ship(world, "T2", _turn("second ask", "echo: second ask"))

    rows = list(Message.objects.filter(session=world["session"]).order_by("turn_index")
                .values_list("plaintext", "turn_index"))
    texts = [t for t, _ in rows if t and t != "ok"]
    assert texts == ["first ask", "echo: first ask", "second ask", "echo: second ask"]
    # The reload shows both conversations, in order.
    body = world["client"].get(f"/api/canopy-sessions/{world['session'].pk}").json()
    shown = [m["plaintext"] for m in body["messages"] if m["role"] in ("user", "assistant")]
    assert shown == ["first ask", "echo: first ask", "second ask", "echo: second ask"]
    # Live: every row went out once, under its stored turn_index.
    live = [m["event"] for m in published if m.get("type") == "chat.turn_event"]
    stored = dict(Message.objects.filter(session=world["session"])
                  .values_list("pk", "turn_index"))
    assert len(live) == 8
    assert all(stored[int(e["message_id"])] == e["turn_index"] for e in live)
    assert len({e["message_id"] for e in live}) == 8
