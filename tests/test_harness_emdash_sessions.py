"""The emdash session controller — reported sessions + the list the phone reads."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


def _ws(slug, owner):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


def _runner(pairer, ws):
    return Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", paired_by=pairer, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )


def _report(client, runner_id, sessions):
    return client.post(
        f"/api/harness/runners/{runner_id}/sessions",
        {"sessions": sessions},
        content_type="application/json",
    )


def test_report_is_wholesale_and_upserts_a_binding_for_continue():
    from django.test import Client

    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)
    c = Client()
    c.force_login(jj)

    r1 = _report(c, runner.id, [
        {"emdash_task": "cloud-runner", "project": "canopy-web", "status": "in_progress",
         "last_interacted_at": "2026-07-16T15:52:00Z"},
        {"emdash_task": "ddd", "project": "canopy-web", "status": "in_progress",
         "last_interacted_at": "2026-07-16T12:41:00Z"},
    ])
    assert r1.status_code == 200, r1.content
    assert RunnerBinding.objects.filter(runner=runner).count() == 2
    binding = RunnerBinding.objects.get(runner=runner, session_key="cloud-runner")
    assert binding.session.origin == Session.ORIGIN_RUNNER
    # The RunnerBinding IS the continue substrate now (SessionLink fold, Plan 3
    # Task 2): this SAME row carries thread_key="emdash:<task>" + host, so a
    # phone-dispatched Continue resolves onto it directly — no second row.
    assert binding.thread_key == "emdash:cloud-runner"
    assert binding.host == runner.host

    # A re-report with one session gone stops its liveness clock (durable, not deleted,
    # and it still knows its runner).
    r2 = _report(c, runner.id, [
        {"emdash_task": "cloud-runner", "project": "canopy-web", "status": "in_progress",
         "last_interacted_at": "2026-07-16T15:59:00Z"},
    ])
    assert r2.status_code == 200
    still_reported = RunnerBinding.objects.get(runner=runner, session_key="cloud-runner")
    dropped = RunnerBinding.objects.get(session_key="ddd")
    # Both keep their runner: identity is durable. Only the liveness CLOCK diverges —
    # the re-reported one is bumped, the dropped one is frozen and will age out of
    # `state=active` once it passes SESSION_LIVE_WINDOW.
    assert dropped.runner_id == runner.id
    assert dropped.live_seen_at < still_reported.live_seen_at
    assert Session.objects.filter(runner_binding=dropped).exists()


def test_report_dedupes_duplicate_task_names_keeping_newest():
    """emdash task names are NOT unique — a report can carry two open sessions that
    share a name. The report must dedupe, keeping the newest (the runner sends
    newest-first, so the first occurrence wins) — regression: two "mobile" tasks
    stranded the whole list, 2026-07-20."""
    from django.test import Client

    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)
    c = Client()
    c.force_login(jj)

    resp = _report(c, runner.id, [
        {"emdash_task": "mobile", "project": "canopy-web", "status": "in_progress",
         "last_interacted_at": "2026-07-20T20:20:00Z"},
        {"emdash_task": "mobile", "project": "canopy-web", "status": "done",
         "last_interacted_at": "2026-07-17T15:48:00Z"},
    ])
    assert resp.status_code == 200, resp.content
    rows = RunnerBinding.objects.filter(runner=runner)
    assert rows.count() == 1
    kept = rows.get()
    assert kept.session_key == "mobile"
    assert kept.status == "in_progress"  # the newer (first) namesake won


def test_a_non_owner_cannot_report_for_another_users_runner():
    from django.test import Client

    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)
    mallory = _user("mallory")
    _ws("mallory-space", mallory)
    c = Client()
    c.force_login(mallory)

    resp = _report(c, runner.id, [{"emdash_task": "x", "project": "canopy-web"}])
    assert resp.status_code == 404
    assert RunnerBinding.objects.filter(runner=runner).count() == 0


def _reported(task, **kw):
    from types import SimpleNamespace

    defaults = dict(project="canopy-web", status="in_progress", last_interacted_at=None,
                     recent_messages=[])
    defaults.update(kw)
    return SimpleNamespace(emdash_task=task, **defaults)


def test_list_is_tenant_scoped_and_hides_offline_runners():
    from datetime import timedelta
    from django.test import Client
    from apps.harness.services import replace_reported_sessions

    jj = _user("jj")
    ws = _ws("dimagi", jj)
    live = _runner(jj, ws)
    replace_reported_sessions(live, ws, [_reported("cloud-runner")])

    # An offline runner's session is hidden (not deleted).
    stale = Runner.objects.create(
        name="old-mbp", kind=Runner.EMDASH, host="old", paired_by=jj, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now() - timedelta(hours=2),
    )
    replace_reported_sessions(stale, ws, [_reported("ghost")])

    c = Client()
    c.force_login(jj)
    rows = c.get("/api/harness/sessions").json()
    tasks = {r["emdash_task"] for r in rows}
    assert tasks == {"cloud-runner"}  # ghost hidden: its runner is not live

    # A non-member sees nothing.
    mallory = _user("mallory")
    _ws("mallory-space", mallory)
    mc = Client()
    mc.force_login(mallory)
    assert mc.get("/api/harness/sessions").json() == []


def test_list_auto_joins_a_domain_matching_user_with_no_membership_row():
    """A @dimagi.com user who has never hit any other endpoint has NO explicit
    WorkspaceMembership row yet. The flat GET /api/harness/sessions path is
    never touched by WorkspaceResolveMiddleware's tenant-prefix auto-join (that
    only fires for /api/w/{ws}/... paths), so list_visible_sessions must call
    wsvc.auto_join_workspaces itself — mirroring list_turns — or a fresh
    domain-matching teammate gets an empty list instead of their workspace's
    sessions."""
    from django.test import Client
    from apps.harness.services import replace_reported_sessions

    owner = _user("owner")
    ws = _ws("dimagi", owner)
    ws.auto_join_domains = ["dimagi.com"]
    ws.save(update_fields=["auto_join_domains"])
    runner = _runner(owner, ws)
    replace_reported_sessions(runner, ws, [_reported("cloud-runner")])

    newcomer = _user("newcomer")  # newcomer@dimagi.com, no WorkspaceMembership row
    assert not WorkspaceMembership.objects.filter(user=newcomer).exists()

    c = Client()
    c.force_login(newcomer)
    rows = c.get("/api/harness/sessions").json()
    tasks = {r["emdash_task"] for r in rows}
    assert tasks == {"cloud-runner"}


def test_list_is_newest_first_by_last_interacted_at():
    from datetime import timedelta
    from django.test import Client
    from apps.harness.services import replace_reported_sessions

    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)
    replace_reported_sessions(runner, ws, [
        _reported("older", last_interacted_at=timezone.now() - timedelta(hours=1)),
        _reported("newer", last_interacted_at=timezone.now()),
    ])

    c = Client()
    c.force_login(jj)
    rows = c.get("/api/harness/sessions").json()
    tasks = [r["emdash_task"] for r in rows]
    assert tasks == ["newer", "older"]
