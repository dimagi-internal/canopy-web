"""A session's subagents reach the chat: the runner posts the set, the binding
caches it, open chats hear the change, and a chat opened later reads it."""
from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SUB = {"agent_id": "a1", "tool_use_id": "toolu_1", "parent_agent_id": "",
       "description": "fix the tests", "agent_type": "general-purpose", "background": True,
       "state": "running", "steps": 3, "last": "Bash · make test",
       "started_at": "2026-10-20T10:00:00Z", "updated_at": "2026-10-20T10:04:00Z"}


def _setup():
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(user=jj, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = Runner.objects.create(name="box", kind=Runner.CLOUD, host="box", owner=jj,
                                   workspace=ws, status=Runner.ONLINE,
                                   last_heartbeat_at=timezone.now())
    session = Session.objects.create(workspace=ws, created_by=jj, origin=Session.ORIGIN_WEB,
                                     title="chat")
    binding = RunnerBinding.objects.create(session=session, runner=runner, host="box",
                                           session_key="k", thread_key=str(session.id))
    client = Client()
    client.force_login(jj)
    return runner, session, binding, client


def _post(client, runner, session, subagents):
    return client.post(f"/api/harness/runners/{runner.id}/session-subagents",
                       {"session_id": str(session.id), "subagents": subagents},
                       content_type="application/json")


def test_posted_subagents_are_what_the_session_detail_serves():
    runner, session, binding, client = _setup()
    out = _post(client, runner, session, [{**SUB, "extra": "dropped"}])
    assert out.json() == {"ok": True}
    binding.refresh_from_db()
    assert binding.subagents == [SUB]
    served = client.get(f"/api/canopy-sessions/{session.id}").json()
    assert served["subagents"] == [SUB]


def test_open_chats_hear_only_a_change():
    runner, session, _binding, client = _setup()
    with mock.patch("apps.realtime.groups.publish") as publish, \
            mock.patch("django.db.transaction.on_commit", side_effect=lambda fn: fn()):
        _post(client, runner, session, [SUB])
        _post(client, runner, session, [SUB])  # the same set again
        _post(client, runner, session, [{**SUB, "state": "done"}])
    frames = [c.args[1] for c in publish.call_args_list
              if c.args[1].get("type") == "session.subagents"]
    assert [f["subagents"][0]["state"] for f in frames] == ["running", "done"]


def test_a_runner_that_does_not_hold_the_session_writes_nothing():
    runner, session, binding, client = _setup()
    other = Runner.objects.create(name="other", kind=Runner.CLOUD, host="other",
                                  owner=runner.owner, workspace=runner.workspace)
    assert _post(client, other, session, [SUB]).json() == {"ok": False}
    binding.refresh_from_db()
    assert binding.subagents == []


def test_a_sensitive_conversation_keeps_no_words():
    runner, session, binding, client = _setup()
    with mock.patch("apps.harness.sensitivity.is_sensitive", return_value=True):
        _post(client, runner, session, [SUB])
    binding.refresh_from_db()
    [kept] = binding.subagents
    assert (kept["description"], kept["last"]) == ("", "")
    assert (kept["state"], kept["steps"]) == ("running", 3)


def test_the_socket_snapshot_and_its_agui_spelling_carry_them():
    from apps.canopy_sessions import agui, serializers

    _runner, session, binding, _client = _setup()
    binding.subagents = [SUB]
    binding.save()
    session.refresh_from_db()
    assert serializers.session_subagents(session) == [SUB]
    [event] = agui.project({"event": "session.subagents", "data": {"subagents": [SUB]}},
                             thread_id=str(session.id))
    assert event.value == {"subagents": [SUB]}
