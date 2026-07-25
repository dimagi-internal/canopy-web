"""Retirement is reversible.

It used to be a one-way door: `_runner_or_404` 404s a retired runner, so its daemon's
heartbeat/claim/report calls all fail forever, and `pair_runner` unconditionally
CREATES a row — so the only recovery minted a NEW runner id and orphaned every
RunnerBinding, assignment and session pointing at the old one. Retiring a laptop you
happened to be logged out of therefore destroyed its sessions' identity the moment you
brought the box back (labs 2026-07-25: jj-mbp-cdp, 10 sessions).
"""
from __future__ import annotations

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
    c = Client()
    c.force_login(user)
    runner = Runner.objects.create(
        name="jj-mbp-cdp", kind=Runner.EMDASH, workspace=ws, paired_by=user,
        host="jj@mbp", status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    return user, ws, c, runner


def test_unretire_brings_a_retired_runner_back():
    _user, _ws, c, runner = _ctx()
    assert c.post(f"/api/harness/runners/{runner.id}/retire").status_code == 204
    assert c.get("/api/harness/runners/").json() == []

    resp = c.post(f"/api/harness/runners/{runner.id}/unretire")

    assert resp.status_code == 200
    assert {r["name"] for r in c.get("/api/harness/runners/").json()} == {"jj-mbp-cdp"}


def test_unretire_keeps_the_same_identity_so_bindings_survive():
    """The whole point: re-pairing would mint a new id and orphan these rows."""
    user, ws, c, runner = _ctx()
    session = Session.objects.create(workspace=ws, origin=Session.ORIGIN_RUNNER, title="ddd")
    RunnerBinding.objects.create(
        session=session, runner=runner, session_key="ddd", host=runner.host,
        live_seen_at=timezone.now(),
    )
    c.post(f"/api/harness/runners/{runner.id}/retire")

    c.post(f"/api/harness/runners/{runner.id}/unretire")

    binding = RunnerBinding.objects.get(session_key="ddd")
    assert binding.runner_id == runner.id, "same row, same id — nothing orphaned"


def test_unretire_restores_disconnected_not_online():
    """Liveness is OBSERVED, never asserted: the next heartbeat makes it online. A
    runner that came back as ONLINE with no heartbeat would look available to the
    claim cascade and swallow turns it cannot run."""
    _user, _ws, c, runner = _ctx()
    Runner.objects.filter(pk=runner.pk).update(last_heartbeat_at=None)
    c.post(f"/api/harness/runners/{runner.id}/retire")

    c.post(f"/api/harness/runners/{runner.id}/unretire")

    runner.refresh_from_db()
    assert runner.status == Runner.DISCONNECTED
    assert runner.live_status == Runner.DISCONNECTED
    assert runner.is_available is False


def test_unretire_is_idempotent_on_a_live_runner():
    _user, _ws, c, runner = _ctx()
    resp = c.post(f"/api/harness/runners/{runner.id}/unretire")
    assert resp.status_code == 200
    runner.refresh_from_db()
    assert runner.status == Runner.ONLINE, "a live runner is not demoted"


def test_a_stranger_cannot_unretire_someone_elses_runner():
    """include_retired widens the LOOKUP, never the visibility predicate."""
    _user, _ws, c, runner = _ctx()
    c.post(f"/api/harness/runners/{runner.id}/retire")

    stranger = Client()
    stranger.force_login(User.objects.create_user("s", "s@example.org", "pw"))
    resp = stranger.post(f"/api/harness/runners/{runner.id}/unretire")

    assert resp.status_code == 404
    assert Runner.objects.get(pk=runner.id).status == Runner.RETIRED  # untouched


def test_retired_runners_stay_invisible_everywhere_else():
    """Only /unretire reaches a retired runner — the widened lookup must not leak."""
    _user, _ws, c, runner = _ctx()
    c.post(f"/api/harness/runners/{runner.id}/retire")

    for path in ("drills", "streams", "backfills"):
        assert c.get(f"/api/harness/runners/{runner.id}/{path}").status_code == 404
    assert c.post(f"/api/harness/runners/{runner.id}/retire").status_code == 404
