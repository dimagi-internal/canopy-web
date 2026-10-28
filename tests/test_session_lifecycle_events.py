"""Every session transition is ONE `session.*` event in its workspace's log, with
ids only (`apps/canopy_sessions/lifecycle.py`).

The bar: an admin can answer "what happened to session X?" from the event log
alone — when it started and stopped, and every change to who can see it or where
it shows up — without reading a word of it. Each test makes one transition and
asserts exactly one event of its kind, carrying the actor and no content.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import lifecycle
from apps.canopy_sessions import placement as session_placement
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Message, RunnerBinding, Session, SessionParticipant
from apps.common import request_context
from apps.events.models import Event
from apps.harness.models import Runner, Turn
from apps.harness.session_sync import replace_reported_sessions
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

TITLE = "SECRET-TITLE do not leak"
WORDS = "SECRET-WORDS from the conversation"


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(user)
    return user, ws, c


def _events(kind=None, ws=None):
    qs = Event.objects.filter(source=lifecycle.SOURCE).order_by("first_seen_at", "pk")
    if kind:
        qs = qs.filter(kind=kind)
    if ws is not None:
        qs = qs.filter(workspace_id=getattr(ws, "slug", ws))
    return list(qs)


def _one(kind, ws=None):
    rows = _events(kind, ws)
    assert len(rows) == 1, [(e.kind, e.payload) for e in _events()]
    return rows[0]


def _no_content():
    """No event anywhere carries a title or a conversation's words."""
    for e in _events():
        blob = json.dumps(e.payload) + e.summary
        assert "SECRET" not in blob, (e.kind, blob)


def _web(ws, user, **kw):
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title=TITLE, **kw)
    Message.objects.create(session=s, turn_index=0, role=Message.USER, plaintext=WORDS)
    return s


def test_creating_a_session_over_rest_is_one_created_event_naming_the_person():
    user, ws, c = _ctx()
    r = c.post("/api/canopy-sessions/", {"title": TITLE}, content_type="application/json")
    assert r.status_code == 200, r.content
    e = _one(lifecycle.CREATED, ws)
    assert e.payload["session"] == r.json()["id"]
    assert e.payload["actor"] == {"kind": "user", "user_id": user.pk, "email": "jj@dimagi.com"}
    assert e.payload["workspace"] == "w1" and e.payload["origin"] == "web"
    assert e.key == "" and e.level == "info"
    # The creator's owner row is a participant event; nothing else.
    assert {x.kind for x in _events()} == {lifecycle.CREATED, lifecycle.PARTICIPANT_ADDED}
    _no_content()


def test_a_runner_discovered_session_is_created_by_the_runner():
    user, ws, _c = _ctx()
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL, host="mbp", owner=user)
    replace_reported_sessions(runner, ws, [SimpleNamespace(emdash_task="t", project="p", status="running",
                                                           last_interacted_at=None, recent_messages=[])],
                              complete=True)
    e = _one(lifecycle.CREATED, ws)
    assert e.payload["actor"]["kind"] == "runner"
    assert e.payload["origin"] == "runner"


def test_archive_then_unarchive_are_one_event_each():
    user, ws, c = _ctx()
    s = _web(ws, user)
    Event.objects.all().delete()
    assert c.post(f"/api/canopy-sessions/{s.id}/archive").status_code == 200
    assert c.post(f"/api/canopy-sessions/{s.id}/archive").status_code == 200   # idempotent: no second event
    e = _one(lifecycle.ARCHIVED, ws)
    assert e.payload["change"] == {"field": "status", "before": "active", "after": "archived"}
    assert e.payload["actor"]["user_id"] == user.pk
    assert c.post(f"/api/canopy-sessions/{s.id}/unarchive").status_code == 200
    _one(lifecycle.UNARCHIVED, ws)
    assert len(_events()) == 2
    _no_content()


def test_closing_a_web_session_is_a_close_not_an_archive():
    user, ws, c = _ctx()
    s = _web(ws, user)
    Event.objects.all().delete()
    assert c.post(f"/api/canopy-sessions/{s.id}/close").json()["ok"] is True
    e = _one(lifecycle.CLOSED, ws)
    assert e.payload["actor"]["email"] == "jj@dimagi.com"
    assert _events(lifecycle.ARCHIVED) == []


def test_closing_a_reported_session_says_who_asked_then_the_runner_closes_it():
    """The close is relayed to the box; its report retires the row. The first event
    keeps the person who asked — the second can only name the box."""
    user, ws, c = _ctx()
    runner = Runner.objects.create(name="jj-mbp", kind="laptop", host="jj-mbp", owner=user, workspace=ws,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    s = _web(ws, user)
    RunnerBinding.objects.create(session=s, runner=runner, host=runner.host, session_key="ddd",
                                 thread_key="emdash:ddd", live_seen_at=timezone.now(),
                                 reported_at=timezone.now())
    Event.objects.all().delete()
    with patch("apps.realtime.groups.publish"):
        assert c.post(f"/api/canopy-sessions/{s.id}/close").json()["closing"] is True
    asked = _one(lifecycle.CLOSE_REQUESTED, ws)
    assert asked.payload["actor"]["user_id"] == user.pk
    assert _events(lifecycle.CLOSED) == []
    replace_reported_sessions(runner, ws, [], archived=["ddd"], complete=False)
    closed = _one(lifecycle.CLOSED, ws)
    assert closed.payload["actor"] == {"kind": "runner", "runner": str(runner.pk), "runner_name": "jj-mbp"}
    assert closed.payload["reason"] == "archived on its runner"
    _no_content()


def test_a_session_gone_from_two_complete_reports_is_closed_by_its_runner():
    user, ws, _c = _ctx()
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL, host="mbp", owner=user)
    task = SimpleNamespace(emdash_task="t", project="p", status="running", last_interacted_at=None,
                           recent_messages=[])
    replace_reported_sessions(runner, ws, [task], complete=True)
    replace_reported_sessions(runner, ws, [], complete=True)
    assert _events(lifecycle.CLOSED) == []
    replace_reported_sessions(runner, ws, [], complete=True)
    e = _one(lifecycle.CLOSED, ws)
    assert e.payload["actor"]["kind"] == "runner"
    # Reported again: it comes back, and that is said too.
    replace_reported_sessions(runner, ws, [task], complete=True)
    _one(lifecycle.UNARCHIVED, ws)


def test_moving_a_session_is_told_to_both_workspaces():
    user, ws, c = _ctx()
    other = Workspace.objects.create(slug="w2", display_name="W2", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=other, role=WorkspaceMembership.OWNER)
    s = _web(ws, user)
    Event.objects.all().delete()
    r = c.post(f"/api/canopy-sessions/{s.id}/move", {"workspace": "w2"}, content_type="application/json")
    assert r.status_code == 200, r.content
    left, joined = _one(lifecycle.MOVED, ws), _one(lifecycle.MOVED, other)
    for e in (left, joined):
        assert e.payload["change"] == {"field": "workspace", "before": "w1", "after": "w2"}
        assert e.payload["session"] == str(s.id)
    _no_content()


def test_participants_added_re_roled_and_removed_are_one_event_each():
    user, ws, c = _ctx()
    mate = User.objects.create_user("mate", "mate@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=mate, workspace=ws, role=WorkspaceMembership.EDITOR)
    s = chat.create_session(workspace=ws, created_by=user, title=TITLE)
    Event.objects.all().delete()
    r = c.post(f"/api/canopy-sessions/{s.id}/participants", {"email": "mate@dimagi.com", "role": "editor"},
               content_type="application/json")
    assert r.status_code == 200, r.content
    added = _one(lifecycle.PARTICIPANT_ADDED, ws)
    assert added.payload["change"]["after"] == {"user_id": mate.pk, "role": "editor"}
    assert added.payload["actor"]["user_id"] == user.pk
    row = SessionParticipant.objects.get(session=s, user=mate)
    row.role = SessionParticipant.VIEWER
    row.save(update_fields=["role"])
    changed = _one(lifecycle.PARTICIPANT_ROLE_CHANGED, ws)
    assert (changed.payload["change"]["before"], changed.payload["change"]["after"]) == ("editor", "viewer")
    assert c.delete(f"/api/canopy-sessions/{s.id}/participants/{mate.pk}").status_code == 200
    removed = _one(lifecycle.PARTICIPANT_REMOVED, ws)
    assert removed.payload["change"]["before"]["user_id"] == mate.pk


def test_slack_bind_detach_and_rebind_each_say_so():
    user, ws, _c = _ctx()
    s = _web(ws, user)
    Event.objects.all().delete()
    s.metadata = {**s.metadata, "slack_thread": "T1:C1:111", "slack_channel": "C1"}
    s.save(update_fields=["metadata", "updated_at"])
    _one(lifecycle.SLACK_BOUND, ws)
    s.metadata = {**s.metadata, "slack_thread": "T1:C1:222"}
    s.save(update_fields=["metadata", "updated_at"])
    _one(lifecycle.SLACK_REBOUND, ws)
    from apps.slack import share

    with patch("apps.slack.relay.session_destination", return_value=None):
        assert share.detach_session(user, s).status == share.DETACHED
    e = _one(lifecycle.SLACK_DETACHED, ws)
    assert e.payload["change"] == {"field": "slack_thread", "before": "T1:C1:222", "after": ""}
    assert len(_events()) == 3


def test_an_agent_turn_closing_its_conversation_names_the_agent_and_the_turn():
    """The Trellis case: an agent's own login, in a turn, closes the conversation."""
    user, ws, _c = _ctx()
    agent = Agent.objects.create(slug="trellis", name="Trellis", owner=user, workspace=ws)
    agent.user = User.objects.create_user("trellis_login", "trellis@dimagi-ai.com", "pw")
    agent.save(update_fields=["user"])
    s = _web(ws, user, agent=agent)
    turn = Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_SLACK)
    Event.objects.all().delete()
    from apps.canopy_sessions import control

    with request_context.bound({"user_id": agent.user.pk,
                                "credential": {"type": "caller_token", "id": 1, "turn_id": str(turn.pk)}}):
        assert control.close_session(session=s) == "closed"
    e = _one(lifecycle.CLOSED, ws)
    assert e.payload["actor"] == {"kind": "agent", "agent": "trellis", "user_id": agent.user.pk}
    assert e.payload["turn"] == str(turn.pk)
    assert e.payload["agent"] == "trellis"
    assert e.summary == f"session {str(s.id)[:8]} closed by trellis"


def test_a_sensitive_session_keeps_only_its_id_and_the_transition():
    user, ws, c = _ctx()
    agent = Agent.objects.create(slug="hal", name="Hal", owner=user, workspace=ws)
    s = _web(ws, user, agent=agent)
    Event.objects.all().delete()
    chat.add_runner_requirements(s, {"sensitive_data"})
    e = _one(lifecycle.SENSITIVITY_CHANGED, ws)
    assert e.payload["change"] == {"field": "sensitive", "before": False, "after": True}
    s.refresh_from_db()
    s.status = Session.ARCHIVED
    s.save(update_fields=["status", "updated_at"])
    a = _one(lifecycle.ARCHIVED, ws)
    assert set(a.payload) <= {"session", "workspace", "actor", "turn", "via", "mcp_tool", "request_id",
                              "sensitive", "reason", "change"}
    assert a.payload["sensitive"] is True and "agent" not in a.payload


def test_deleting_a_session_is_one_event_and_its_participants_go_quietly():
    user, ws, _c = _ctx()
    s = chat.create_session(workspace=ws, created_by=user, title=TITLE)
    Event.objects.all().delete()
    sid = str(s.id)
    s.delete()
    e = _one(lifecycle.DELETED, ws)
    assert e.payload["session"] == sid
    # The participant row went with its session: that is the deletion, not a removal.
    assert _events(lifecycle.PARTICIPANT_REMOVED) == []


def test_reset_fork_and_transfer_are_one_event_each():
    user, ws, c = _ctx()
    SESSION_CAPABLE = {"sessions": True, "projects": ["canopy-web"]}
    runner = Runner.objects.create(name="jj-mbp", workspace=ws, kind=Runner.EMDASH, location=Runner.LOCAL,
                                   status=Runner.ONLINE, owner=user, host="jj@mbp",
                                   capabilities=SESSION_CAPABLE, engine=Runner.ENGINE_CLAUDE_CLI)
    cloud = Runner.objects.create(name="cloud-ec2-1", workspace=ws, kind=Runner.CLOUD, status=Runner.ONLINE,
                                  owner=user, host="cloud-ec2-1", capabilities=SESSION_CAPABLE)
    s = Session.objects.create(workspace=ws, project="canopy-web", title=TITLE, created_by=user,
                               metadata={chat.TRANSCRIPT_SOURCED: True})
    RunnerBinding.objects.create(session=s, runner=runner, session_key="src-sid", emdash_project="canopy-web",
                                 host=runner.host, thread_key=str(s.id))
    Message.objects.create(session=s, turn_index=0, role=Message.USER, plaintext=WORDS)
    Message.objects.create(session=s, turn_index=64, role=Message.ASSISTANT, plaintext=WORDS)
    Event.objects.all().delete()

    r = c.post(f"/api/canopy-sessions/{s.id}/fork", {"turn_index": 64}, content_type="application/json")
    assert r.status_code == 200, r.content
    forked = _one(lifecycle.FORKED, ws)
    assert forked.payload["fork"] == r.json()["id"] and forked.payload["session"] == str(s.id)
    _one(lifecycle.CREATED, ws)     # the fork itself, born

    with patch.object(chat, "_reset_blocker", return_value=(chat.RESET_OK, s.runner_binding)), \
            patch("apps.canopy_sessions.transcript_read.request_backfill"):
        assert chat.reset_session(s)["ok"]
    assert _one(lifecycle.RESET, ws).payload["rows_dropped"] == 2

    session_placement.transfer_session(session=s, placement=str(cloud.id))
    moved = _one(lifecycle.TRANSFERRED, ws)
    assert moved.payload["change"] == {"field": "runner", "before": str(runner.id), "after": str(cloud.id)}
    _no_content()


def test_a_turn_token_names_its_turns_conversation():
    """`agent.canopy_token.issued` carried `session: ""` for every turn-scoped login,
    whose turn's conversation was nevertheless known."""
    from apps.harness import turn_agent_tokens

    user, ws, _c = _ctx()
    agent = Agent.objects.create(slug="trellis", name="Trellis", owner=user, workspace=ws)
    agent.user = User.objects.create_user("trellis_login", "trellis@dimagi-ai.com", "pw")
    agent.save(update_fields=["user"])
    s = _web(ws, user, agent=agent)
    runner = Runner.objects.create(name="cloud-ec2-live", workspace=ws, kind=Runner.CLOUD, owner=user, host="c")
    turn = Turn.objects.create(chat_session=s, origin="slack")
    turn_agent_tokens.issue(turn, runner)
    e = Event.objects.get(kind="agent.canopy_token.issued")
    assert e.payload["session"] == str(s.id)
    assert e.payload["scope"] == "turn"
