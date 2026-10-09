"""A reused emdash task NAME is not the same session (hal board task T74).

The report keys a binding on (runner, project, task name), and emdash names are
reused over time: close "supply", open another "supply", and the second task is a
different conversation under the same key. The runner now reports emdash's own
task id (`task_uid`); when the id behind a name changes, the new task gets its own
Session and the old record is archived with its rows intact. Everything that
continues ONE task (`claude --resume`, `--continue`, `/clear`) keeps the id, so it
never forks.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.canopy_sessions.services import TASK_UID_KEY
from apps.harness.models import Runner
from apps.harness.services import replace_reported_sessions
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def ws(django_user_model):
    op = django_user_model.objects.create_user(username="op", email="op@x.org")
    ws = Workspace.objects.create(slug="t74", display_name="T74", created_by=op)
    WorkspaceMembership.objects.create(user=op, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture
def runner(ws):
    return Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL, host="mbp",
                                 owner=ws.created_by, status=Runner.ONLINE)


def _reported(task, uid="", project="supply-repo"):
    s = SimpleNamespace(emdash_task=task, project=project, status="running",
                        last_interacted_at=None, recent_messages=[])
    if uid:
        s.task_uid = uid
    return s


def _report(runner, ws, *sessions):
    replace_reported_sessions(runner, ws, list(sessions), complete=True)


def _binding(task="supply"):
    return RunnerBinding.objects.select_related("session").get(session_key=task)


def _row(session, text, index=0):
    Message.objects.create(session=session, role=Message.USER, plaintext=text,
                           turn_index=index)


def test_a_new_task_under_a_reused_name_gets_its_own_session(runner, ws):
    _report(runner, ws, _reported("supply", "task-a"))
    first = _binding().session
    _row(first, "the first conversation")
    # Closed and reopened under the same name between two reports — the window
    # the archive-then-revive fork (fork_if_name_reused) could never see.
    _report(runner, ws, _reported("supply", "task-b"))

    second = _binding().session
    assert second.pk != first.pk
    assert second.status == Session.ACTIVE and second.title == "supply"
    assert second.parent_session_id == first.pk
    first.refresh_from_db()
    assert first.status == Session.ARCHIVED
    assert first.metadata[TASK_UID_KEY] == "task-a"
    assert list(first.messages.values_list("plaintext", flat=True)) == ["the first conversation"]
    assert not second.messages.exists()
    b = _binding()
    assert b.task_uid == "task-b" and b.transcript_id == ""


def test_the_same_task_reported_again_never_forks(runner, ws):
    """resume/continue/clear inside one task keep its id: one record throughout."""
    _report(runner, ws, _reported("supply", "task-a"))
    first = _binding().session
    for _ in range(3):
        _report(runner, ws, _reported("supply", "task-a"))
    assert _binding().session_id == first.pk
    assert Session.objects.count() == 1


def test_an_old_runner_with_no_id_changes_nothing(runner, ws):
    _report(runner, ws, _reported("supply"))
    first = _binding().session
    _report(runner, ws, _reported("supply"))
    assert _binding().session_id == first.pk and _binding().task_uid == ""


def test_the_first_id_on_a_legacy_binding_is_adopted_not_forked(runner, ws):
    _report(runner, ws, _reported("supply"))
    first = _binding().session
    _report(runner, ws, _reported("supply", "task-a"))
    assert _binding().session_id == first.pk and _binding().task_uid == "task-a"
    # ...and a blank afterwards (a runner downgraded) is no claim either.
    _report(runner, ws, _reported("supply"))
    assert _binding().session_id == first.pk and _binding().task_uid == "task-a"


def test_two_live_namesakes_alternating_rebind_their_own_records(runner, ws):
    """Two un-archived tasks can share a name; the report keeps only the newest,
    so which one is reported can flip. Each flip must return to that task's own
    record, never mint a third."""
    _report(runner, ws, _reported("supply", "task-a"))
    a = _binding().session
    _report(runner, ws, _reported("supply", "task-b"))
    b = _binding().session
    _report(runner, ws, _reported("supply", "task-a"))
    assert _binding().session_id == a.pk
    a.refresh_from_db(); b.refresh_from_db()
    assert a.status == Session.ACTIVE and b.status == Session.ARCHIVED
    _report(runner, ws, _reported("supply", "task-b"))
    assert _binding().session_id == b.pk
    assert Session.objects.count() == 2


def test_a_closed_then_reused_name_forks_on_the_report(runner, ws):
    """The old path (archive, then revive) also forks by id now, before any ship —
    so the revived-then-forked dance is not needed when the id is known."""
    _report(runner, ws, _reported("supply", "task-a"))
    first = _binding().session
    _report(runner, ws)
    _report(runner, ws)
    first.refresh_from_db()
    assert first.status == Session.ARCHIVED
    _report(runner, ws, _reported("supply", "task-b"))
    first.refresh_from_db()
    assert first.status == Session.ARCHIVED
    assert "reopened_unconfirmed" not in first.metadata
    second = _binding().session
    assert second.pk != first.pk and second.status == Session.ACTIVE
    assert "reopened_unconfirmed" not in second.metadata


def test_the_report_endpoint_accepts_the_id(runner, ws):
    from django.test import Client

    c = Client()
    c.force_login(ws.created_by)
    for uid in ("task-a", "task-b"):
        resp = c.post(f"/api/harness/runners/{runner.id}/sessions", data={
            "sessions": [{"emdash_task": "supply", "project": "supply-repo", "task_uid": uid}],
            "complete": True,
        }, content_type="application/json")
        assert resp.status_code == 200, resp.content
    assert Session.objects.filter(runner_binding__isnull=True).count() == 1
    assert _binding().task_uid == "task-b"
