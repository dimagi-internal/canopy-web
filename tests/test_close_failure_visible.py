"""A failed close is visible in canopy, not only in the runner's log.

2026-10-12: "Close sessions" from the menu bar left three sessions open, and the
only place the reason existed was ~/.canopy/runner.log in another macOS user's
home folder. The runner now reports each close's outcome; a failure is recorded
on the session (`RunnerBinding.close_error`), shown with it in Supervisor, and
cleared by a Retry, a later success, or the task no longer being reported.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions import control as chat_control
from apps.canopy_sessions.models import RunnerBinding
from apps.harness.models import Runner
from apps.harness.session_sync import replace_reported_sessions
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


def _ws(slug, owner):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


def _runner(owner, ws, name="jj-mbp", host="jj-mac"):
    return Runner.objects.create(
        name=name, kind=Runner.EMDASH, host=host, owner=owner, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )


def _reported(task, project="canopy-web"):
    return SimpleNamespace(emdash_task=task, project=project, status="in_progress",
                           last_interacted_at=None, recent_messages=[])


def _post(client, runner, results):
    r = client.post(f"/api/harness/runners/{runner.id}/close-results",
                    data={"results": results}, content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()


def test_a_failed_close_shows_until_it_is_retried_succeeds_or_the_task_is_gone():
    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)
    both = [_reported("chat-1"), _reported("chat-1", project="other")]
    replace_reported_sessions(runner, ws, both)
    binding = RunnerBinding.objects.get(session_key="chat-1", emdash_project="canopy-web")
    namesake = RunnerBinding.objects.get(session_key="chat-1", emdash_project="other")
    binding.reported_at = binding.live_seen_at          # a runner is holding the task
    binding.save(update_fields=["reported_at"])

    client = Client()
    client.force_login(jj)

    out = _post(client, runner, [{"session_key": "chat-1", "project": "canopy-web",
                                  "ok": False, "error": "emdash unreachable over CDP"}])
    assert out["updated"] == 1
    binding.refresh_from_db()
    namesake.refresh_from_db()
    assert binding.close_error == "emdash unreachable over CDP"
    assert binding.close_error_at is not None
    assert namesake.close_error == "", "another project's same-named task is not this one"

    # The ~10s report re-saves every binding it touches; it must not wipe it.
    replace_reported_sessions(runner, ws, both)
    binding.refresh_from_db()
    assert binding.close_error == "emdash unreachable over CDP"

    # The session payload carries it — that is all Supervisor reads.
    body = client.get("/api/canopy-sessions/?state=all").json()
    rows = body["sessions"] if isinstance(body, dict) else body
    row = next(r for r in rows if r["id"] == str(binding.session_id))
    assert row["close_error"] == "emdash unreachable over CDP"
    assert row["close_error_at"]

    # Retry is a new close request: it starts clean, and the runner sees the
    # server's (now blank) reason, so it reports again if this attempt fails.
    assert chat_control.close_session(session=binding.session) == "closing"
    binding.refresh_from_db()
    assert binding.close_error == "" and binding.close_requested
    closes = client.get(f"/api/harness/runners/{runner.id}/closes").json()["closes"]
    assert [(c["session_key"], c["close_error"]) for c in closes] == [("chat-1", "")]

    # A later success clears it...
    _post(client, runner, [{"session_key": "chat-1", "project": "canopy-web",
                            "ok": False, "error": "x"}])
    _post(client, runner, [{"session_key": "chat-1", "project": "canopy-web", "ok": True}])
    binding.refresh_from_db()
    assert binding.close_error == "" and binding.close_error_at is None

    # ...and so does the task no longer being reported.
    _post(client, runner, [{"session_key": "chat-1", "project": "canopy-web",
                            "ok": False, "error": "x"}])
    replace_reported_sessions(runner, ws, [_reported("chat-1", project="other")])
    binding.refresh_from_db()
    assert binding.close_error == ""


def test_a_session_with_no_project_is_reported_against_its_blank_project():
    """The menu bar's skipped sessions (no project, never deleted by name alone)."""
    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)
    replace_reported_sessions(runner, ws, [_reported("orphan", project=""),
                                           _reported("orphan", project="ace")])
    client = Client()
    client.force_login(jj)
    _post(client, runner, [{"session_key": "orphan", "project": "", "ok": False,
                            "error": "couldn't tell which project's task this is"}])
    assert RunnerBinding.objects.get(emdash_project="").close_error.startswith("couldn't tell")
    assert RunnerBinding.objects.get(emdash_project="ace").close_error == ""


def test_close_results_touch_only_the_reporting_runners_sessions():
    jj, other = _user("jj"), _user("other")
    ws = _ws("dimagi", jj)
    mine = _runner(jj, ws)
    theirs = _runner(other, ws, name="o-mbp", host="o-mac")
    replace_reported_sessions(theirs, ws, [_reported("chat-1")])

    client = Client()
    client.force_login(jj)
    out = _post(client, mine, [{"session_key": "chat-1", "project": "canopy-web",
                                "ok": False, "error": "nope"}])
    assert out["updated"] == 0
    assert RunnerBinding.objects.get(runner=theirs).close_error == ""
    # Another person's runner is not reachable at all.
    r = client.post(f"/api/harness/runners/{theirs.id}/close-results",
                    data={"results": []}, content_type="application/json")
    assert r.status_code == 404
