"""The live user frame carries the author too, not just the durable row.

Server-side attribution (apps/canopy_sessions/services.py::persist_transcript_rows,
2026-09-27) finds an unmarked user row's author from the turn it matches — but
that happens in the SAME call that also fans the row out live
(apps/harness/api.py::post_session_stream). Without carrying the result across,
a watching client would see no author until the next reload, exactly the gap
`stream_map`'s marker-based author existed to close for the marked case.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions import stream_map
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness import initiator as who
from apps.harness import services as harness_services
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, owner=user)
    c = Client()
    c.force_login(user)
    return user, ws, runner, c


def _claimed_chat_turn(session, user, *, text):
    turn, _created = harness_services.enqueue_turn(
        session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
        idempotency_key=f"chat:{session.id.hex}:c1", prompt=text,
        initiator=who.for_user(user, via="web", assurance="verified"),
    )
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE, claimed_at=timezone.now())
    return Turn.objects.get(pk=turn.pk)


def test_a_live_user_frame_gains_the_matched_authors(monkeypatch):
    user, ws, runner, c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    RunnerBinding.objects.create(session=s, runner=runner, session_key="echo-1", stream_desired=True)
    turn = _claimed_chat_turn(s, user, text="ship it")

    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    body = c.post(
        f"/api/harness/runners/{runner.id}/session-stream",
        data={"session_id": str(s.id),
              "events": [{"kind": "user", "seq": 0, "index": 0, "payload": {"text": "ship it"}}]},
        content_type="application/json",
    ).json()
    assert body == {"count": 1}
    msg = Message.objects.get(session=s)
    assert msg.author == {"name": "Jonathan", "user_id": user.id}
    assert msg.source_turn_id == turn.pk

    (_group, frame) = published[0]
    assert frame["event"]["payload"]["author"] == {"name": "Jonathan", "user_id": user.id}


def test_a_line_typed_straight_into_emdash_carries_no_author(monkeypatch):
    user, ws, runner, c = _ctx()
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    RunnerBinding.objects.create(session=s, runner=runner, session_key="echo-1", stream_desired=True)

    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    c.post(
        f"/api/harness/runners/{runner.id}/session-stream",
        data={"session_id": str(s.id),
              "events": [{"kind": "user", "seq": 0, "index": 0, "payload": {"text": "typed in emdash"}}]},
        content_type="application/json",
    )
    (_group, frame) = published[0]
    assert "author" not in frame["event"]["payload"]


# -- stream_map's fallback (a live frame with no marker, given an author) ----

def test_stream_map_uses_the_payload_author_when_unmarked():
    frames = stream_map.turn_event_to_frames(
        {"kind": "user", "seq": 5,
         "payload": {"text": "hi", "author": {"name": "A", "user_id": 1}}},
        lambda _s: "m1",
    )
    assert frames[0]["data"]["author"] == {"name": "A", "user_id": 1}
    assert frames[0]["data"]["plaintext"] == "hi"


def test_stream_map_prefers_a_parsed_marker_over_the_payload_author():
    from apps.canopy_sessions.testing import legacy_marker

    marked = legacy_marker("hi", name="Marker Author", user_id=99, turn_id="3f2a9c1e0b7d4c55a1e2f3a4b5c6d7e8")
    frames = stream_map.turn_event_to_frames(
        {"kind": "user", "seq": 5,
         "payload": {"text": marked, "author": {"name": "Ignored", "user_id": 1}}},
        lambda _s: "m1",
    )
    assert frames[0]["data"]["author"] == {"name": "Marker Author", "user_id": 99}
    assert frames[0]["data"]["plaintext"] == "hi"
