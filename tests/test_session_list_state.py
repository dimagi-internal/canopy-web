"""`state=active` is the union of two rules: not explicitly archived, AND seen by a
runner recently. Only a session no runner has ever held is exempt from the second —
it has no runner to be seen by, so absence says nothing about it."""
import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.canopy_sessions.services import SESSION_LIVE_WINDOW
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(user)
    runner = Runner.objects.create(
        name="jj-air", workspace=ws, location=Runner.LOCAL, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), paired_by=user,
    )
    return user, ws, c, runner


def _runner_session(ws, runner, key, seen_ago: dt.timedelta):
    s = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title=key)
    RunnerBinding.objects.create(
        session=s, runner=runner, session_key=key,
        last_interacted_at=timezone.now() - seen_ago,
        live_seen_at=timezone.now() - seen_ago,
    )
    return s


def _bound_web_session(ws, user, runner, key, seen_ago: dt.timedelta, stamped=True):
    """A chat STARTED on the web that has since been sent — so a runner spun up an
    emdash session for it and reports it like any other. `stamped=False` is the legacy
    row whose binding predates the liveness clock."""
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title=key
    )
    RunnerBinding.objects.create(
        session=s, runner=runner, session_key=key,
        last_interacted_at=timezone.now() - seen_ago,
        live_seen_at=(timezone.now() - seen_ago) if stamped else None,
    )
    return s


def test_active_hides_an_explicitly_archived_session():
    user, ws, c, runner = _ctx()
    fresh = _runner_session(ws, runner, "live", dt.timedelta(minutes=1))
    gone = _runner_session(ws, runner, "closed", dt.timedelta(minutes=1))
    Session.objects.filter(pk=gone.pk).update(status=Session.ARCHIVED)

    ids = {r["id"] for r in c.get("/api/canopy-sessions/").json()}
    assert ids == {str(fresh.id)}


def test_active_hides_a_runner_session_unseen_past_the_cutoff():
    user, ws, c, runner = _ctx()
    fresh = _runner_session(ws, runner, "live", SESSION_LIVE_WINDOW - dt.timedelta(seconds=30))
    stale = _runner_session(ws, runner, "vanished", SESSION_LIVE_WINDOW + dt.timedelta(minutes=1))

    ids = {r["id"] for r in c.get("/api/canopy-sessions/").json()}
    assert ids == {str(fresh.id)}, "the just-inside-cutoff session must survive"
    assert str(stale.id) not in ids


def test_an_unbound_web_session_never_goes_stale():
    """A web chat that has never been sent has no runner, so 'unseen by a runner' is
    meaningless for it. Only an explicit archive ends one."""
    user, ws, c, _runner = _ctx()
    old = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web")
    Session.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - SESSION_LIVE_WINDOW - dt.timedelta(days=30)
    )
    ids = {r["id"] for r in c.get("/api/canopy-sessions/").json()}
    assert str(old.id) in ids


def test_a_bound_web_session_goes_stale_when_its_runner_stops_reporting_it():
    """Where a chat STARTED says nothing about whether it is still open. Once a web
    chat has been sent, a runner holds an emdash session for it and reports it every
    ~10s — so absence from that report is the same direct observation it is for a
    runner-discovered session. Exempting it left labs showing 10 week-old phone chats
    as live, whose emdash tasks had been deleted days earlier (2026-07-31)."""
    user, ws, c, runner = _ctx()
    fresh = _bound_web_session(ws, user, runner, "still-open",
                               SESSION_LIVE_WINDOW - dt.timedelta(seconds=30))
    gone = _bound_web_session(ws, user, runner, "task-deleted",
                              SESSION_LIVE_WINDOW + dt.timedelta(days=6))

    ids = {r["id"] for r in c.get("/api/canopy-sessions/").json()}
    assert str(fresh.id) in ids, "a web chat its runner still reports must stay active"
    assert str(gone.id) not in ids

    archived = {r["id"] for r in c.get("/api/canopy-sessions/?state=archived").json()}
    assert str(gone.id) in archived, "retired, never deleted — it must still be reachable"


def test_a_bound_web_session_never_stamped_is_stale_not_fresh():
    """A binding predating the liveness clock has no stamp at all. Both writers stamp
    unconditionally now, so an unstamped binding means 'never reported', not 'reported
    at an unknown time' — and unknown must not read as live."""
    user, ws, c, runner = _ctx()
    legacy = _bound_web_session(ws, user, runner, "legacy", dt.timedelta(days=7),
                                stamped=False)
    ids = {r["id"] for r in c.get("/api/canopy-sessions/").json()}
    assert str(legacy.id) not in ids


def test_archived_and_all_return_the_complements():
    user, ws, c, runner = _ctx()
    fresh = _runner_session(ws, runner, "live", dt.timedelta(minutes=1))
    stale = _runner_session(ws, runner, "vanished", SESSION_LIVE_WINDOW + dt.timedelta(minutes=1))

    archived = {r["id"] for r in c.get("/api/canopy-sessions/?state=archived").json()}
    assert archived == {str(stale.id)}

    every = {r["id"] for r in c.get("/api/canopy-sessions/?state=all").json()}
    assert every == {str(fresh.id), str(stale.id)}


def test_an_unknown_state_is_422_not_a_silent_full_list():
    user, ws, c, runner = _ctx()
    assert c.get("/api/canopy-sessions/?state=bogus").status_code == 422


def test_limit_applies_after_the_running_first_sort():
    """A queryset slice would order by -created_at and could cut the running row; the
    limit must bite AFTER the sort that actually decides what matters."""
    user, ws, c, runner = _ctx()
    # Created FIRST and running now. The queryset orders by -created_at, so a
    # queryset-level slice would drop exactly this row — the one the sort floats.
    live = _runner_session(ws, runner, "live", dt.timedelta(seconds=5))
    # Created SECOND (so newest by created_at) but idle — a slice would keep it.
    _idle = _runner_session(ws, runner, "idle", dt.timedelta(hours=2))

    rows = c.get("/api/canopy-sessions/?limit=1").json()
    assert [r["id"] for r in rows] == [str(live.id)]
