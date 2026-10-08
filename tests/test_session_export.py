"""Exporting a session's conversation so the person who started it can hand it
to their own Claude and carry on.

Built server-side from what canopy already holds — the user and assistant
`Message` rows the person saw — never the runner's raw transcript. And only the
session's starter may take it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.canopy_sessions.models import Message, RunnerBinding, Session, SessionParticipant
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(name="jj-mbp-cdp", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, owner=user, capabilities={"sessions": True})
    s = Session.objects.create(workspace=ws, project="canopy-web", title="widget", created_by=user)
    RunnerBinding.objects.create(session=s, runner=runner, session_key="a-task",
                                 emdash_project="canopy-web", thread_key=str(s.id))
    rows = [(0, Message.USER, "Let's fix the widget's empty state.", {}),
            (64, Message.TOOL_USE, "", {"name": "Read", "input": {"file_path": "apps/widget.py"}}),
            (65, Message.TOOL_RESULT, "x" * 5000, {"tool_use_id": "t1"}),
            (128, Message.ASSISTANT, "Fixed — it now shows a prompt instead of a blank panel.", {}),
            (130, Message.SYSTEM, "runner paused", {}),
            (192, Message.USER, "Great, now the error state ✓", {})]
    for i, role, text, content in rows:
        Message.objects.create(session=s, turn_index=i, role=role, plaintext=text,
                               content=content)
    c = Client()
    c.force_login(user)
    return user, ws, s, c


def _export(c, s):
    return c.get(f"/api/canopy-sessions/{s.id}/export")


def test_the_starter_gets_the_session_as_the_web_view_shows_it():
    _u, _ws, s, c = _ctx()
    resp = _export(c, s)
    assert resp.status_code == 200
    out = resp.json()
    md = out["markdown"]
    assert out["message_count"] == 6
    # Every row the web view shows, in its order — tool calls included, so the
    # reader can see what was actually done.
    order = [md.index("Let's fix the widget's empty state."),
             md.index("→ `Read` {\"file_path\": \"apps/widget.py\"}"),
             md.index("x" * 100),
             md.index("Fixed — it now shows a prompt"),
             md.index("_runner paused_"),
             md.index("Great, now the error state ✓")]
    assert order == sorted(order)
    # Tool output is shortened, as the web view collapses it.
    assert "x" * 1600 not in md and "…" in md
    assert "check git" in md.split("## ", 1)[0]


def test_an_editor_who_did_not_start_it_cannot_export():
    _u, ws, s, _c = _ctx()
    other = User.objects.create_user("e", "e@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=other, workspace=ws, role=WorkspaceMembership.EDITOR)
    SessionParticipant.objects.create(session=s, user=other, role=SessionParticipant.EDITOR)
    oc = Client()
    oc.force_login(other)
    assert oc.get(f"/api/canopy-sessions/{s.id}").status_code == 200   # can see and drive it
    assert _export(oc, s).status_code == 403                              # but not take it


def test_a_session_nobody_started_cannot_be_exported_yet():
    """Runner-discovered sessions have no starter; admins are a later step."""
    _u, _ws, s, c = _ctx()
    Session.objects.filter(pk=s.pk).update(created_by=None, origin=Session.ORIGIN_RUNNER)
    assert _export(c, s).status_code in (403, 404)


def test_a_non_member_cannot_see_it_at_all():
    _u, _ws, s, _c = _ctx()
    stranger = User.objects.create_user("x", "x@dimagi.com", "pw")
    sc = Client()
    sc.force_login(stranger)
    assert _export(sc, s).status_code == 404


def test_an_empty_conversation_is_a_409_not_an_empty_file():
    _u, _ws, s, c = _ctx()
    s.messages.all().delete()
    assert _export(c, s).status_code == 409


def test_it_never_asks_the_runner_for_anything():
    """Answered from canopy's own rows, so it works with the runner offline,
    paused or retired — the usual reason to export."""
    _u, _ws, s, c = _ctx()
    Runner.objects.all().update(status=Runner.RETIRED)
    assert _export(c, s).status_code == 200
