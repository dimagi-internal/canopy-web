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


def test_the_report_corrects_an_autotitled_session():
    """A session created on the phone is autotitled (first message, truncated)
    before any emdash task exists, so it kept a sentence for a name while the
    sidebar showed the task. The first fix went into `record_session`, which only
    runs when a TURN is routed — this is the path that runs every ~10s, and
    missing it is why the repair never happened (observed 2026-07-27, after
    shipping it)."""
    from django.test import Client

    from apps.canopy_sessions.models import Message, RunnerBinding, Session

    jj = _user("jj-title")
    ws = _ws("dimagi-title", jj)
    runner = _runner(jj, ws)
    title = "I think we basically implemented everything"
    session = Session.objects.create(workspace=ws, created_by=jj, title=title)
    Message.objects.create(session=session, turn_index=0, role=Message.USER,
                           plaintext=title)
    RunnerBinding.objects.create(session=session, runner=runner,
                                 session_key="canopy-web-api-7716",
                                 thread_key="emdash:canopy-web-api-7716",
                                 host=runner.host)
    c = Client(); c.force_login(jj)
    _report(c, runner.id, [{"emdash_task": "canopy-web-api-7716", "project": "canopy-web"}])
    session.refresh_from_db()
    assert session.title == "canopy-web-api-7716"


def test_the_report_never_clobbers_a_title_a_human_chose():
    from django.test import Client

    from apps.canopy_sessions.models import RunnerBinding, Session

    jj = _user("jj-human")
    ws = _ws("dimagi-human", jj)
    runner = _runner(jj, ws)
    session = Session.objects.create(workspace=ws, created_by=jj, title="Ship the release")
    RunnerBinding.objects.create(session=session, runner=runner,
                                 session_key="canopy-web-api-7716",
                                 thread_key="emdash:canopy-web-api-7716",
                                 host=runner.host)
    c = Client(); c.force_login(jj)
    _report(c, runner.id, [{"emdash_task": "canopy-web-api-7716", "project": "canopy-web"}])
    session.refresh_from_db()
    assert session.title == "Ship the release"


def test_a_failing_retitle_never_breaks_the_session_report(monkeypatch):
    """The report is how canopy knows which sessions are alive. Shipping the
    retitle unguarded 500'd the WHOLE report on labs, so every session on that
    runner stopped being reported — a cosmetic nicety took out the liveness
    signal for the entire machine.

    Simulated by making the repair itself raise: the report must still succeed
    and still upsert the binding."""
    from django.test import Client

    from apps.canopy_sessions.models import RunnerBinding, Session
    from apps.harness import services as hsvc

    jj = _user("jj-safe")
    ws = _ws("dimagi-safe", jj)
    runner = _runner(jj, ws)
    session = Session.objects.create(workspace=ws, created_by=jj, title="A sentence")
    RunnerBinding.objects.create(session=session, runner=runner,
                                 session_key="task-x",
                                 thread_key="emdash:task-x", host=runner.host)

    def boom(*a, **k):
        raise RuntimeError("retitle exploded")

    monkeypatch.setattr(hsvc, "_title_is_derived", boom)
    c = Client(); c.force_login(jj)
    resp = _report(c, runner.id, [{"emdash_task": "task-x", "project": "canopy-web"}])
    assert resp.status_code == 200, "a broken retitle must not fail the report"
    # And the liveness half still happened.
    binding = RunnerBinding.objects.get(session=session)
    assert binding.live_seen_at is not None


# NOT TESTED HERE, deliberately — and worth recording why.
#
# The production failure was:
#   TransactionManagementError: select_for_update cannot be used outside of a
#   transaction
# raised by POST /runners/{id}/sessions on labs, every ~10s, taking the liveness
# signal for a whole machine down with it (2026-07-27).
#
# I wrote a test for it, then removed the fix to check the test actually caught
# the bug. It did not — it passed either way. pytest-django wraps each test in a
# transaction, and even `django_db(transaction=True)` did not reproduce the
# condition through the test client. A test that passes with the bug present is
# worse than no test: it claims coverage it does not have.
#
# The regression guard that DOES work is `test_a_failing_retitle_never_breaks_
# the_session_report` above: it asserts the report survives any failure in the
# cosmetic path, which is the property that actually matters here regardless of
# which exception is raised.


@pytest.mark.django_db
def test_a_menu_is_cleared_when_its_session_stops_being_reported():
    """A dialog is a claim about a screen. When emdash no longer has the task,
    that screen is gone and the claim has to go with it.

    Observed 2026-08-01: `pending_question` is written only inside the loop over
    the sessions IN a report, so a session whose task had been deleted kept its
    last dialog forever — the API served a phone six buttons, every one of which
    could only fail, and the failure came back as "could not reach emdash on this
    runner", which was not even true. The runner cannot fix this from its side:
    it prunes its own copy, but it has no way to say anything about a session it
    can no longer see. Absence from the wholesale report is the observation.
    """
    from apps.harness.services import replace_reported_sessions

    jj = _user("jj")
    ws = _ws("dimagi", jj)
    runner = _runner(jj, ws)

    replace_reported_sessions(runner, ws, [_reported("gone-soon"), _reported("stays")])
    for key in ("gone-soon", "stays"):
        binding = RunnerBinding.objects.get(session_key=key)
        binding.pending_question = {"question": "pick one", "options": [{"number": 1, "label": "a"}]}
        binding.save()

    # "stays" is still reported AND still asking. Note the report is the fresh
    # observation for anything IN it (a reported session with no `question` is
    # correctly cleared by the loop above) — this clear is only for the sessions
    # the report can no longer say anything about at all.
    still_asking = _reported("stays")
    still_asking.question = {"question": "pick one", "options": [{"number": 1, "label": "a"}]}
    replace_reported_sessions(runner, ws, [still_asking])

    assert RunnerBinding.objects.get(session_key="gone-soon").pending_question is None
    # The one still open keeps its dialog — this must not become "clear them all".
    assert RunnerBinding.objects.get(session_key="stays").pending_question is not None


@pytest.mark.django_db
def test_clearing_a_stale_menu_does_not_touch_another_runners_sessions():
    """The report is wholesale PER RUNNER. Scoping the clear any wider would let
    one box's report retire dialogs on every other box."""
    from apps.harness.services import replace_reported_sessions

    jj = _user("jj")
    ws = _ws("dimagi", jj)
    mine, theirs = _runner(jj, ws), _runner(jj, ws)

    replace_reported_sessions(theirs, ws, [_reported("elsewhere")])
    other = RunnerBinding.objects.get(session_key="elsewhere")
    other.pending_question = {"question": "still up", "options": []}
    other.save()

    replace_reported_sessions(mine, ws, [_reported("mine")])

    assert RunnerBinding.objects.get(session_key="elsewhere").pending_question is not None
