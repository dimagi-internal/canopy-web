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


def test_backfill_asks_the_runner_even_when_rows_exist():
    """Deliberately replaces "ready when rows exist".

    That short-circuit tested `turn_index == 0`, which under the composite
    ordinal scheme means record 0 / block 0 — a summary or a noise-filtered
    harness record, both DROPPED rather than renumbered. So it could not fire on
    a real transcript: verified on labs (2026-07-31), session cf2d5089's oldest
    index after a COMPLETE backfill was 448, and a second click still answered
    `requested`. A branch that is always false is a claim the code never honours,
    and it hid the fact that every click re-shipped the whole transcript.

    Asking unconditionally is cheap in the way that matters — the write is
    ordinal-keyed, so re-shipping held rows costs one probe and zero inserts.
    """
    _u, _w, s, _r, c = _ctx(runner_online=True)
    Message.objects.create(session=s, turn_index=448, role=Message.USER, plaintext="hi")
    assert c.post(f"/api/canopy-sessions/{s.id}/backfill").json() == {"status": "requested"}


def test_session_reports_whether_a_ship_is_still_outstanding():
    """`backfill_pending` is what lets the client wait on an exact signal.

    It previously slept a flat 1200 ms and read once — 13 s early on labs, so the
    button reported success having changed nothing. Watching the row count grow
    instead is no better on its own: an already-complete session never grows, so
    the client would spin for its whole timeout on the common case.
    """
    _u, _w, s, _r, c = _ctx(runner_online=True)
    assert c.get(f"/api/canopy-sessions/{s.id}").json()["backfill_pending"] is False
    c.post(f"/api/canopy-sessions/{s.id}/backfill")
    assert c.get(f"/api/canopy-sessions/{s.id}").json()["backfill_pending"] is True
    # Cleared by the runner's final chunk — mirroring post_session_backfill.
    b = RunnerBinding.objects.get(session=s)
    b.backfill_requested = False
    b.save(update_fields=["backfill_requested"])
    assert c.get(f"/api/canopy-sessions/{s.id}").json()["backfill_pending"] is False


def test_backfill_pending_is_false_for_a_session_with_no_runner():
    """No binding means nothing can be in flight — the client must not wait."""
    user = User.objects.create_user("solo", "solo@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w9", display_name="W9", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    s = Session.objects.create(workspace=ws, created_by=user, title="web only")
    c = Client(); c.force_login(user)
    assert c.get(f"/api/canopy-sessions/{s.id}").json()["backfill_pending"] is False


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
