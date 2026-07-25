import datetime as dt
import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    c = Client(); c.force_login(user)
    return user, ws, c


def test_list_unions_web_and_runner_sessions():
    user, ws, c = _ctx()
    web = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web")
    # a runner-discovered session: no created_by, but it has a binding
    disc = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="disc")
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(), paired_by=user)
    RunnerBinding.objects.create(session=disc, runner=runner, session_key="echo-1",
                                 last_interacted_at=timezone.now(), live_seen_at=timezone.now())
    body = c.get("/api/canopy-sessions/").json()
    ids = {row["id"] for row in body}
    assert ids == {str(web.id), str(disc.id)}   # BOTH origins, one row each


def test_list_row_carries_liveness():
    user, ws, c = _ctx()
    disc = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="disc")
    runner = Runner.objects.create(name="jj-air", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(), paired_by=user)
    RunnerBinding.objects.create(session=disc, runner=runner, session_key="echo-1",
                                 last_interacted_at=timezone.now(), live_seen_at=timezone.now())
    row = next(r for r in c.get("/api/canopy-sessions/").json() if r["id"] == str(disc.id))
    assert row["origin"] == "runner"
    assert row["running"] is True                # runner online + fresh interaction
    assert row["runner_name"] == "jj-air"
    assert row["runner_location"] == "local"
    assert row["session_key"] == "echo-1"


def test_idle_when_runner_offline_or_stale():
    user, ws, c = _ctx()
    disc = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="disc")
    # runner never heartbeated -> live_status != ONLINE
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.DISCONNECTED, paired_by=user)
    RunnerBinding.objects.create(session=disc, runner=runner, session_key="echo-1",
                                 last_interacted_at=timezone.now(), live_seen_at=timezone.now())
    row = next(r for r in c.get("/api/canopy-sessions/").json() if r["id"] == str(disc.id))
    assert row["running"] is False
    assert row["runner_name"] == "laptop"        # still shown, just not "running"


def test_web_session_without_binding_is_idle():
    user, ws, c = _ctx()
    web = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="web")
    row = next(r for r in c.get("/api/canopy-sessions/").json() if r["id"] == str(web.id))
    assert row["origin"] == "web"
    assert row["running"] is False
    assert row["runner_name"] is None
    assert row["runner_location"] is None


def test_running_sorts_first():
    user, ws, c = _ctx()
    idle = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="idle")
    live = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="live")
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(), paired_by=user)
    RunnerBinding.objects.create(session=live, runner=runner, session_key="echo-1",
                                 last_interacted_at=timezone.now(), live_seen_at=timezone.now())
    body = c.get("/api/canopy-sessions/").json()
    ids = [r["id"] for r in body]
    assert body[0]["id"] == str(live.id)         # running row floats to the top
    assert str(idle.id) in ids                   # the idle web session is still listed, below it


def test_dedup_web_session_with_live_binding_appears_once():
    # A session that is BOTH created_by=me AND has a live RunnerBinding matches
    # BOTH halves of the Q(created_by=...) | Q(runner_binding__isnull=False)
    # union — this is exactly the overlap the .distinct() call must collapse.
    # Without .distinct(), the join through runner_binding (a OneToOne, but
    # still a join) would return this session id twice in `rows`.
    user, ws, c = _ctx()
    mine_and_live = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="mine-and-live",
    )
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(), paired_by=user)
    RunnerBinding.objects.create(session=mine_and_live, runner=runner, session_key="echo-1",
                                 last_interacted_at=timezone.now(), live_seen_at=timezone.now())
    body = c.get("/api/canopy-sessions/").json()
    matches = [row for row in body if row["id"] == str(mine_and_live.id)]
    assert len(matches) == 1                    # would be 2 if .distinct() were dropped


def test_list_does_not_leak_other_users_or_workspaces():
    # Mine: my workspace, my web session.
    user, ws, c = _ctx()
    mine = Session.objects.create(workspace=ws, created_by=user, origin=Session.ORIGIN_WEB, title="mine")

    stranger = User.objects.create_user("other", "other@dimagi.com", "pw")

    # (a) a stranger's UNBOUND web session in MY workspace (different creator,
    # no RunnerBinding) — must not appear: it fails both halves of the union
    # (not created_by=me, and no binding).
    WorkspaceMembership.objects.create(user=stranger, workspace=ws, role=WorkspaceMembership.EDITOR)
    stranger_same_ws = Session.objects.create(
        workspace=ws, created_by=stranger, origin=Session.ORIGIN_WEB, title="stranger-same-ws",
    )

    # (b) a stranger's runner-bound session in a DIFFERENT workspace I'm not a
    # member of — must not appear: it satisfies the binding half of the union
    # but fails the workspace__in=slugs tenant filter.
    other_ws = Workspace.objects.create(slug="w2", display_name="W2", created_by=stranger)
    WorkspaceMembership.objects.create(user=stranger, workspace=other_ws, role=WorkspaceMembership.OWNER)
    stranger_other_ws = Session.objects.create(
        workspace=other_ws, origin=Session.ORIGIN_RUNNER, title="stranger-other-ws",
    )
    other_runner = Runner.objects.create(name="other-laptop", workspace=other_ws, location=Runner.LOCAL,
                                         status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                         paired_by=stranger)
    RunnerBinding.objects.create(session=stranger_other_ws, runner=other_runner, session_key="echo-2",
                                 last_interacted_at=timezone.now(), live_seen_at=timezone.now())

    body = c.get("/api/canopy-sessions/").json()
    ids = {row["id"] for row in body}
    assert ids == {str(mine.id)}
    assert str(stranger_same_ws.id) not in ids
    assert str(stranger_other_ws.id) not in ids


def test_running_false_when_runner_online_but_interaction_stale():
    # Runner IS online, but last_interacted_at is older than RUNNING_WINDOW
    # (120s) -> is_session_running's freshness check must return False. This
    # exercises the half of the derivation the offline test doesn't cover.
    user, ws, c = _ctx()
    disc = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="disc")
    runner = Runner.objects.create(name="laptop", workspace=ws, location=Runner.LOCAL,
                                   status=Runner.ONLINE, last_heartbeat_at=timezone.now(), paired_by=user)
    stale_ts = timezone.now() - dt.timedelta(seconds=150)  # RUNNING_WINDOW (120s) + 30
    RunnerBinding.objects.create(session=disc, runner=runner, session_key="echo-1",
                                 last_interacted_at=stale_ts, live_seen_at=timezone.now())
    row = next(r for r in c.get("/api/canopy-sessions/").json() if r["id"] == str(disc.id))
    assert row["running"] is False
    assert row["runner_name"] == "laptop"        # still shown, just not "running"
