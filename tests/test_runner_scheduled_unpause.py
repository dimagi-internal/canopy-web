"""A runner whose Claude subscription is capped pauses itself until the reset.

The pause is the ordinary operator pause — one state, shown and enforced the same
way. What a capped runner adds is a SCHEDULED UNPAUSE (`Runner.unpause_at`), run
by the server on its own clock (`services.wake_due_runners`, every heartbeat from
any runner) — never by the parked runner, which fires nothing while paused, and
never as an agent turn, which would spend the tokens that just ran out.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.harness import services
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
        name="jj-mbp-cdp", kind=Runner.EMDASH, workspace=ws, owner=user,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    return user, ws, c, runner


def _pause(c, runner, **body):
    return c.post(f"/api/harness/runners/{runner.id}/pause", data=body,
                  content_type="application/json")


def _in(minutes):
    return timezone.now() + dt.timedelta(minutes=minutes)


def test_a_capped_runner_is_an_ordinary_pause_with_an_unpause_scheduled():
    _u, _ws, c, runner = _ctx()
    resp = _pause(c, runner, note="Claude usage cap — resumes 08:30Z", until=_in(50).isoformat())
    assert resp.status_code == 200
    body = resp.json()
    assert body["paused"] is True
    assert body["status"] == Runner.PAUSED
    assert body["unpause_at"] is not None
    runner.refresh_from_db()
    assert runner.live_status == Runner.PAUSED


def test_it_stays_paused_until_the_scheduled_unpause_runs():
    _u, _ws, c, runner = _ctx()
    _pause(c, runner, note="cap", until=_in(50).isoformat())
    assert services.wake_due_runners(timezone.now()) == set()
    runner.refresh_from_db()
    assert runner.paused is True


def test_the_server_sweep_unpauses_it_at_the_reset():
    _u, _ws, c, runner = _ctx()
    _pause(c, runner, note="cap", until=_in(50).isoformat())
    woken = services.wake_due_runners(_in(51))
    assert woken == {runner.pk}
    runner.refresh_from_db()
    assert (runner.paused, runner.paused_note, runner.unpause_at) == (False, "", None)
    assert runner.live_status == Runner.ONLINE


def test_any_runners_heartbeat_wakes_a_parked_box_and_its_own_beat_reports_it():
    user, ws, c, runner = _ctx()
    other = Runner.objects.create(name="cloud-1", kind=Runner.EMDASH, workspace=ws, owner=user,
                                  status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    Runner.objects.filter(pk=runner.pk).update(
        paused=True, paused_note="cap", unpause_at=timezone.now() - dt.timedelta(seconds=1))

    services.heartbeat(other, active_turn_ids=[])
    runner.refresh_from_db()
    assert runner.paused is False

    # And when the beat is the parked box's own, the heartbeat RESPONSE says so —
    # the laptop mirrors it down and deletes ~/.canopy/PAUSED.
    Runner.objects.filter(pk=runner.pk).update(
        paused=True, paused_note="cap", unpause_at=timezone.now() - dt.timedelta(seconds=1))
    runner.refresh_from_db()
    out = services.heartbeat(runner, active_turn_ids=[])
    assert out.paused is False


def test_an_operator_pause_is_never_touched_by_the_sweep():
    _u, _ws, c, runner = _ctx()
    _pause(c, runner, note="parked by jj")
    assert services.wake_due_runners(_in(60 * 24 * 30)) == set()
    services.heartbeat(runner, active_turn_ids=[])
    runner.refresh_from_db()
    assert runner.paused is True


def test_a_cap_does_not_put_a_clock_on_an_operators_open_ended_pause():
    _u, _ws, c, runner = _ctx()
    _pause(c, runner, note="parked by jj")
    body = _pause(c, runner, note="cap", until=_in(50).isoformat()).json()
    assert body["unpause_at"] is None
    assert body["paused_note"] == "parked by jj"


def test_an_operator_pause_cancels_the_scheduled_unpause():
    _u, _ws, c, runner = _ctx()
    _pause(c, runner, note="cap", until=_in(50).isoformat())
    body = _pause(c, runner, note="keep it parked").json()
    assert body["unpause_at"] is None
    assert services.wake_due_runners(_in(51)) == set()


def test_a_second_cap_only_moves_the_unpause_later():
    _u, _ws, c, runner = _ctx()
    later = _in(120)
    _pause(c, runner, note="weekly cap", until=later.isoformat())
    body = _pause(c, runner, note="session cap", until=_in(30).isoformat()).json()
    assert dt.datetime.fromisoformat(body["unpause_at"]) == later


def test_unpause_cancels_the_schedule_too():
    _u, _ws, c, runner = _ctx()
    _pause(c, runner, note="cap", until=_in(50).isoformat())
    body = c.post(f"/api/harness/runners/{runner.id}/unpause").json()
    assert (body["paused"], body["unpause_at"]) == (False, None)


def test_a_reset_already_in_the_past_does_not_park_the_box():
    _u, _ws, c, runner = _ctx()
    body = _pause(c, runner, note="cap", until=_in(-5).isoformat()).json()
    assert body["paused"] is False
