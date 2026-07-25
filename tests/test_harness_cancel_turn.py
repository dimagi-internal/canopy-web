"""Cancelling a queued turn — the composer's take-it-back, and the only API
path to retire a misfired turn before a runner claims it."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.agents.models import Agent
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _ws(slug, owner):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture
def jj(db):
    return _user("jj")


@pytest.fixture
def canopy(db, jj):
    return _ws("canopy", jj)


@pytest.fixture
def cli(client, jj, canopy):
    client.force_login(jj)
    return client


def test_cancel_a_queued_project_turn(cli, canopy):
    turn = Turn.objects.create(
        project="canopy-web", workspace=canopy, origin=Turn.ORIGIN_MANUAL, idempotency_key="k1"
    )
    resp = cli.post(f"/api/harness/turns/{turn.id}/cancel")

    assert resp.status_code == 200, resp.content
    turn.refresh_from_db()
    assert turn.status == Turn.CANCELLED
    assert "cancelled" in turn.result_note


def test_cancel_a_queued_agent_turn(cli, canopy):
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=canopy)
    turn = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_MANUAL, idempotency_key="k1")

    assert cli.post(f"/api/harness/turns/{turn.id}/cancel").status_code == 200
    turn.refresh_from_db()
    assert turn.status == Turn.CANCELLED


def test_cannot_cancel_a_running_turn(cli, canopy):
    """A running turn is live in an emdash session — the runner owns its lease.
    Cancel is un-queue, not kill."""
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=canopy)
    turn = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_MANUAL, idempotency_key="k1", status=Turn.RUNNING
    )
    resp = cli.post(f"/api/harness/turns/{turn.id}/cancel")

    assert resp.status_code == 409
    turn.refresh_from_db()
    assert turn.status == Turn.RUNNING  # untouched


def test_cancel_is_idempotent(cli, canopy):
    turn = Turn.objects.create(
        project="canopy-web", workspace=canopy, origin=Turn.ORIGIN_MANUAL, idempotency_key="k1"
    )
    assert cli.post(f"/api/harness/turns/{turn.id}/cancel").status_code == 200
    # second cancel: already FAILED (terminal) -> still 200, no error
    assert cli.post(f"/api/harness/turns/{turn.id}/cancel").status_code == 200


def test_a_cancelled_turn_is_not_claimable(cli, canopy, jj):
    """The point of cancel: the turn must never be picked up after."""
    from apps.harness import services
    from django.utils import timezone

    turn = Turn.objects.create(
        project="canopy-web", workspace=canopy, origin=Turn.ORIGIN_MANUAL, idempotency_key="k1"
    )
    cli.post(f"/api/harness/turns/{turn.id}/cancel")

    runner = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={"projects": ["canopy-web"]},
    )
    assert services.claim_next_turn(runner) is None


def test_cannot_cancel_another_tenants_turn(client, canopy, jj):
    """_turn_or_404 gates the cancel: a non-member gets 404, not a cancel."""
    turn = Turn.objects.create(
        project="canopy-web", workspace=canopy, origin=Turn.ORIGIN_MANUAL, idempotency_key="k1"
    )
    mallory = _user("mallory")
    _ws("mallory-space", mallory)
    client.force_login(mallory)

    resp = client.post(f"/api/harness/turns/{turn.id}/cancel")
    assert resp.status_code == 404
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED  # untouched


# --------------------------------------------------------------------------------------
# services.cancel_turn — the full cancel semantics (chat.stop / the REST stop route,
# task 6+). Queued unqueues immediately; an executing turn is signalled, not
# force-finished — the runner owns its lease.
# --------------------------------------------------------------------------------------


def test_cancel_turn_unqueues_as_cancelled(canopy):
    from apps.harness import services

    turn = Turn.objects.create(
        project="canopy-web", workspace=canopy, origin=Turn.ORIGIN_MANUAL, idempotency_key="k-cancel-1"
    )
    out = services.cancel_turn(turn)
    assert out.status == Turn.CANCELLED


def test_cancel_turn_signals_running_turn(canopy, jj, monkeypatch):
    from django.utils import timezone

    from apps.harness import services

    agent = Agent.objects.create(slug="echo", name="Echo", workspace=canopy)
    runner = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={"agents": ["echo"]},
    )
    turn = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_MANUAL, idempotency_key="k-cancel-2",
        status=Turn.RUNNING, claimed_by=runner,
    )
    published = []
    monkeypatch.setattr("apps.realtime.groups.publish", lambda g, m: published.append((g, m)))

    out = services.cancel_turn(turn)

    assert out.status == turn.status  # unchanged — runner owns the lease
    assert turn.events.filter(kind="cancel_requested").exists()
    assert published and published[0][1]["type"] == "runner.cancel"


def test_sweep_finishes_cancel_requested_as_cancelled(canopy):
    import datetime as dt

    from django.utils import timezone

    from apps.harness import services

    agent = Agent.objects.create(slug="echo", name="Echo", workspace=canopy)
    runner = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    turn = Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_MANUAL, idempotency_key="k-cancel-3",
        status=Turn.RUNNING, claimed_by=runner,
        lease_expires_at=timezone.now() - dt.timedelta(minutes=1),
    )

    services.append_events(turn, [{"kind": "cancel_requested", "payload": {}}])
    services.sweep_expired_leases()

    turn.refresh_from_db()
    assert turn.status == Turn.CANCELLED
