"""One producer per session for chat ROWS.

A transcript-sourced session has two things tailing the same .jsonl: the
per-turn `chat_bridge`, which posts each assistant text as an `assistant`
TurnEvent, and the per-session transcript tailer, which posts every
conversational record to `/session-stream` with its durable composite ordinal.
Both fan out to the same session group, so every assistant reply arrived twice
— once as `seq:<ordinal>`, once as `<turn8>:<ledger seq>` — and the client has
no way to reconcile those (tool rows dedupe on `tool_use_id`, user rows on
their text, an assistant row on nothing). Observed live 2026-08-27 on the
`targeting` session: every reply on screen twice, and once on reload.

So the ledger stops fanning chat rows for a session whose transcript is the
durable source, and keeps fanning turn-lifecycle events, which no transcript
contains. A ledger-sourced session has no transcript to tail and is untouched.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.canopy_sessions.models import Session
from apps.canopy_sessions.services import TRANSCRIPT_SOURCED
from apps.harness import services
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    return user, ws


def _turn(session):
    return Turn.objects.create(
        chat_session=session, origin=Turn.ORIGIN_API,
        idempotency_key=f"k-{session.id}",
    )


def _session_frames(published, session):
    """Just the frames aimed at the per-session group."""
    return [m for g, m in published if g.endswith(session.id.hex)]


def _capture(monkeypatch):
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr("apps.realtime.groups.publish",
                        lambda g, m: published.append((g, m)))
    return published


ROWS_AND_LIFECYCLE = [
    {"kind": "assistant", "payload": {"text": "hi"}},
    {"kind": "user", "payload": {"text": "yo"}},
    {"kind": "tool_start", "payload": {"id": "toolu_1", "name": "Bash"}},
    {"kind": "tool_end", "payload": {"tool_use_id": "toolu_1"}},
    {"kind": "status", "payload": {"status": Turn.RUNNING}},
    {"kind": "error", "payload": {"detail": "boom"}},
]


def test_transcript_sourced_session_does_not_get_the_ledgers_copy_of_chat_rows(monkeypatch):
    """The tailer owns the rows; the ledger keeps the lifecycle.

    This is the whole bug: without the filter, `assistant` goes out here AND
    over /session-stream, under two irreconcilable ids, and the reply doubles.
    """
    _user, ws = _ctx()
    # ORIGIN_RUNNER is transcript-sourced by definition — discovered in emdash,
    # the transcript is all there ever was.
    session = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    turn = _turn(session)
    published = _capture(monkeypatch)

    services.append_events(turn, ROWS_AND_LIFECYCLE)

    kinds = [f["event"]["kind"] for f in _session_frames(published, session)]
    assert kinds == ["status", "error"]


def test_web_created_session_marked_transcript_sourced_is_filtered_too(monkeypatch):
    """Where a chat STARTED says nothing about where its record lives — a web
    composer session driven by a runner writes the same transcript, so it has
    the same two producers and needs the same filter."""
    _user, ws = _ctx()
    session = Session.objects.create(
        workspace=ws, origin=Session.ORIGIN_WEB, title="a",
        metadata={TRANSCRIPT_SOURCED: True},
    )
    turn = _turn(session)
    published = _capture(monkeypatch)

    services.append_events(turn, ROWS_AND_LIFECYCLE)

    kinds = [f["event"]["kind"] for f in _session_frames(published, session)]
    assert kinds == ["status", "error"]


def test_ledger_sourced_session_still_gets_every_event(monkeypatch):
    """The dev stub and pre-unification sessions have no transcript to tail, so
    the ledger is the ONLY producer and dropping its rows would blank the chat."""
    _user, ws = _ctx()
    session = Session.objects.create(workspace=ws, origin=Session.ORIGIN_WEB, title="a")
    turn = _turn(session)
    published = _capture(monkeypatch)

    services.append_events(turn, ROWS_AND_LIFECYCLE)

    kinds = [f["event"]["kind"] for f in _session_frames(published, session)]
    assert kinds == [e["kind"] for e in ROWS_AND_LIFECYCLE]


def test_the_turn_group_keeps_every_event_either_way(monkeypatch):
    """The filter is about the SESSION group only. `turn.{id}` is the turn's own
    ledger feed — the run view reads it, and it must stay complete."""
    _user, ws = _ctx()
    session = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="a")
    turn = _turn(session)
    published = _capture(monkeypatch)

    services.append_events(turn, ROWS_AND_LIFECYCLE)

    turn_kinds = [
        m["event"]["kind"] for g, m in published
        if m.get("type") == "turn.event"
    ]
    assert turn_kinds == [e["kind"] for e in ROWS_AND_LIFECYCLE]
