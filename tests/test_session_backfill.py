import datetime as dt
import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions import services
from apps.canopy_sessions.models import Message, RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx(runner_online=True, has_runner=True):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_RUNNER, title="t")
    r = None
    if has_runner:
        r = Runner.objects.create(
            name="laptop", workspace=ws, location=Runner.LOCAL, paired_by=user,
            status=Runner.ONLINE if runner_online else Runner.DISCONNECTED,
            last_heartbeat_at=timezone.now() if runner_online else None,
        )
    RunnerBinding.objects.create(session=s, runner=r, session_key="echo-1")
    c = Client(); c.force_login(user)
    return user, ws, s, r, c


def test_backfill_requested_when_runner_is_degraded(monkeypatch):
    """A DEGRADED runner is still reporting — it just isn't claiming turns.

    Regression (found on prod): emdash's CDP port was unreachable, so the runner
    self-reported DEGRADED and stopped claiming, but its poll loop kept running
    and backfill only reads a transcript FILE. Gating on ONLINE made history
    permanently 'unavailable' for history the runner could ship.
    """
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_RUNNER, title="t")
    r = Runner.objects.create(
        name="laptop", workspace=ws, location=Runner.LOCAL, paired_by=user,
        status=Runner.DEGRADED, last_heartbeat_at=timezone.now(),  # fresh => live_status == degraded
    )
    RunnerBinding.objects.create(session=s, runner=r, session_key="echo-1")
    c = Client(); c.force_login(user)

    assert r.live_status == Runner.DEGRADED
    assert c.post(f"/api/canopy-sessions/{s.id}/backfill").json() == {"status": "requested"}
    assert published, "a degraded (but reachable) runner must still be signalled"


def test_backfill_unavailable_when_runner_heartbeat_is_stale():
    """Stale heartbeat => live_status STALE => genuinely unreachable, so unavailable."""
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_RUNNER, title="t")
    r = Runner.objects.create(
        name="laptop", workspace=ws, location=Runner.LOCAL, paired_by=user,
        status=Runner.DEGRADED, last_heartbeat_at=timezone.now() - dt.timedelta(hours=1),
    )
    RunnerBinding.objects.create(session=s, runner=r, session_key="echo-1")
    c = Client(); c.force_login(user)

    assert r.live_status == Runner.STALE
    assert c.post(f"/api/canopy-sessions/{s.id}/backfill").json() == {"status": "unavailable"}


def test_backfill_ready_when_rows_exist():
    _u, _w, s, _r, c = _ctx()
    Message.objects.create(session=s, turn_index=0, role=Message.USER, plaintext="hi")
    assert c.post(f"/api/canopy-sessions/{s.id}/backfill").json() == {"status": "ready"}


def test_backfill_requested_when_runner_live(monkeypatch):
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    _u, _w, s, r, c = _ctx(runner_online=True)
    assert c.post(f"/api/canopy-sessions/{s.id}/backfill").json() == {"status": "requested"}
    assert RunnerBinding.objects.get(session=s).backfill_requested is True
    assert len(published) == 1
    group, frame = published[0]
    assert group.endswith(r.id.hex)                 # the bound runner's control group
    assert frame == {
        "type": "runner.stream",
        "session_id": str(s.id),
        "session_key": "echo-1",
        "desired": None,                            # None marks a backfill ask (not a stream toggle)
    }


def test_backfill_unavailable_when_no_live_runner():
    _u, _w, s, _r, c = _ctx(has_runner=False)
    assert c.post(f"/api/canopy-sessions/{s.id}/backfill").json() == {"status": "unavailable"}


def test_write_backfill_writes_rows_once():
    _u, _w, s, _r, _c = _ctx()
    msgs = [{"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1"}]
    assert services.write_backfill(s, msgs) == 2
    assert [m.plaintext for m in s.messages.order_by("turn_index")] == ["q1", "a1"]
    # second call is a no-op (server-full thereafter)
    assert services.write_backfill(s, msgs) == 0
    assert s.messages.count() == 2


def test_runner_backfill_endpoints(monkeypatch):
    _u, _w, s, r, c = _ctx()
    RunnerBinding.objects.filter(session=s).update(backfill_requested=True)
    # runner syncs its pending backfills
    body = c.get(f"/api/harness/runners/{r.id}/backfills").json()
    assert [b["session_id"] for b in body["backfills"]] == [str(s.id)]
    # runner ships history -> rows written, flag cleared
    resp = c.post(
        f"/api/harness/runners/{r.id}/session-backfill",
        data={"session_id": str(s.id),
              "messages": [{"role": "user", "text": "q"}, {"role": "assistant", "text": "a"}]},
        content_type="application/json",
    ).json()
    assert resp == {"written": 2}
    assert RunnerBinding.objects.get(session=s).backfill_requested is False
    assert s.messages.count() == 2


def test_session_backfill_rejects_unbound_runner():
    _u, ws, s, _r, c = _ctx()
    # a DIFFERENT runner (not the one bound to the session) tries to ship history
    other = Runner.objects.create(name="other", workspace=ws, location=Runner.LOCAL, paired_by=_u)
    resp = c.post(
        f"/api/harness/runners/{other.id}/session-backfill",
        data={"session_id": str(s.id),
              "messages": [{"role": "user", "text": "q"}, {"role": "assistant", "text": "a"}]},
        content_type="application/json",
    )
    assert resp.status_code == 404
    assert s.messages.count() == 0


def test_backfill_requested_when_runner_is_paused(monkeypatch):
    """Same shape as the DEGRADED case above, one state over: a paused runner's
    poll loop runs `drain_backfills` every tick BEFORE the pause gate — pause
    stops starting turns, not shipping files it already has."""
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    s = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_RUNNER, title="t")
    r = Runner.objects.create(
        name="laptop", workspace=ws, location=Runner.LOCAL, paired_by=user,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), paused=True,
    )
    RunnerBinding.objects.create(session=s, runner=r, session_key="echo-1")
    c = Client(); c.force_login(user)

    assert r.live_status == Runner.PAUSED
    assert c.post(f"/api/canopy-sessions/{s.id}/backfill").json() == {"status": "requested"}
    assert published, "a paused (but alive) runner must still be signalled"
