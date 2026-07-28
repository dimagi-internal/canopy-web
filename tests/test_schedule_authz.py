"""The b4f5ead regression: capabilities is a routing hint, NOT a security boundary.

A runner paired by an outsider, declaring a victim agent's slug, must see zero
of that agent's schedules and must not be able to fire them.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import AgentSchedule, Runner, Turn
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SLOT = dt.datetime(2026, 7, 17, 13, tzinfo=dt.UTC)


@pytest.fixture()
def founder():
    """Workspace.created_by is a non-nullable PROTECT FK, so every Workspace needs
    an author. Deliberately NOT either party below, and never made a member:
    authorship must not confer access to anything this test asserts about.
    """
    return User.objects.create_user("founder", "founder@example.org", "pw")


@pytest.fixture()
def victim_ws(founder):
    # auto_join_domains=[] is load-bearing: both users below are @dimagi.com, and
    # a domain auto-join would silently make the attacker a member — the test
    # would pass while testing nothing.
    return Workspace.objects.create(
        slug="dimagi", display_name="Dimagi", auto_join_domains=[], created_by=founder
    )


@pytest.fixture()
def attacker_ws(founder):
    return Workspace.objects.create(
        slug="evilcorp", display_name="Evil Corp", auto_join_domains=[], created_by=founder
    )


@pytest.fixture()
def victim_agent(victim_ws):
    return Agent.objects.create(slug="echo", name="Echo", workspace=victim_ws)


@pytest.fixture()
def victim_schedule(victim_agent):
    return AgentSchedule.objects.create(
        agent=victim_agent, name="Weekly manager report",
        prompt="/echo:manager-report — CONFIDENTIAL",
        cron="0 9 * * 5", timezone="America/New_York",
    )


@pytest.fixture()
def attacker_client(attacker_ws, victim_ws):
    user = User.objects.create_user("mallory", "mallory@dimagi.com", "pw")
    wsvc.ensure_member(attacker_ws, user, WorkspaceMembership.OWNER)
    assert not wsvc.is_member(user, victim_ws.slug)  # guard: the test must mean something
    c = Client()
    c.force_login(user)
    return c


def _pair_and_online(client) -> str:
    """Pair a runner DECLARING THE VICTIM'S AGENT SLUG, then heartbeat it online.

    The heartbeat is NOT load-bearing for these two routes: unlike
    claim_next_turn, sync/fire have no ONLINE gate, so these tests pass
    identically without it. It is kept for realism (a real daemon heartbeats
    before it syncs) and to stay honest if an ONLINE gate is ever added.

    No workspace is set on the Runner — tenancy derives from paired_by, which the
    server assigns from request.user at pairing.
    """
    resp = client.post(
        "/api/harness/runners/",
        {"name": "evil-box", "kind": "emdash", "capabilities": {"agents": ["echo"]}},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    rid = resp.json()["id"]
    hb = client.post(
        f"/api/harness/runners/{rid}/heartbeat",
        {"active_turn_ids": [], "degraded": False, "note": ""},
        content_type="application/json",
    )
    assert hb.status_code == 200, hb.content
    assert Runner.objects.get(pk=rid).status == Runner.ONLINE
    return rid


def test_cross_tenant_sync_leaks_nothing(attacker_client, attacker_ws, victim_schedule):
    rid = _pair_and_online(attacker_client)

    resp = attacker_client.get(f"/api/harness/schedules/?runner_id={rid}")

    assert resp.status_code == 200
    assert resp.json()["items"] == []  # capabilities claimed echo; tenancy denied it


def test_cross_tenant_fire_404s(attacker_client, attacker_ws, victim_schedule):
    rid = _pair_and_online(attacker_client)

    resp = attacker_client.post(
        f"/api/harness/schedules/{victim_schedule.id}/fire?runner_id={rid}",
        {"slot": SLOT.isoformat()},
        content_type="application/json",
    )

    assert resp.status_code == 404  # 404 not 403 — no existence leak
    assert Turn.objects.count() == 0  # and no turn was materialized


@pytest.fixture()
def victim_runner(victim_ws):
    """A runner legitimately paired by a member of the victim's tenant."""
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    wsvc.ensure_member(victim_ws, user, WorkspaceMembership.OWNER)
    client = Client()
    client.force_login(user)
    return _pair_and_online(client)


def test_foreign_runner_id_sync_404s(attacker_client, attacker_ws, victim_schedule, victim_runner):
    """The exploit: the attacker need not spoof paired_by — she CHOOSES whose
    paired_by is read, by passing the victim's runner_id. A runner may only be
    operated by the user who paired it.
    """
    resp = attacker_client.get(f"/api/harness/schedules/?runner_id={victim_runner}")

    assert resp.status_code == 404  # 404 not 403 — non-ownership must not leak existence
    assert b"CONFIDENTIAL" not in resp.content  # the prompt never crosses the boundary


def test_foreign_runner_id_fire_404s(attacker_client, attacker_ws, victim_schedule, victim_runner):
    resp = attacker_client.post(
        f"/api/harness/schedules/{victim_schedule.id}/fire?runner_id={victim_runner}",
        {"slot": SLOT.isoformat()},
        content_type="application/json",
    )

    assert resp.status_code == 404
    assert Turn.objects.count() == 0  # no turn materialized on the victim's agent


def test_orphaned_runner_fails_closed(attacker_client, attacker_ws, victim_schedule, victim_runner):
    """Runner.paired_by is on_delete=SET_NULL, so it can be NULL on an orphaned
    runner. NULL must FAIL CLOSED — never silently match the caller.
    """
    Runner.objects.filter(pk=victim_runner).update(paired_by=None)

    resp = attacker_client.get(f"/api/harness/schedules/?runner_id={victim_runner}")

    assert resp.status_code == 404


def test_sync_limit_zero_does_not_500(victim_ws, victim_schedule, victim_runner):
    """?limit=0 does NOT 422 — the route clamps via max(1, min(limit, 500))
    before it ever reaches Page (whose `limit` is Field(ge=1, le=500)), so an
    out-of-range limit is silently floored to a valid one instead of crashing
    inside the response model. This pins that clamp: 200, with the clamped
    limit reflected in the page and exactly one item returned (the one
    schedule the fixtures set up).
    """
    user = User.objects.get(username="jj")
    client = Client()
    client.force_login(user)

    resp = client.get(f"/api/harness/schedules/?runner_id={victim_runner}&limit=0")

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["limit"] == 1  # max(1, min(0, 500)) == 1
    assert len(body["items"]) == 1
    assert body["total"] == 1


def test_same_tenant_runner_syncs_and_fires(victim_ws, victim_schedule):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    wsvc.ensure_member(victim_ws, user, WorkspaceMembership.OWNER)
    client = Client()
    client.force_login(user)
    rid = _pair_and_online(client)

    listing = client.get(f"/api/harness/schedules/?runner_id={rid}")
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1

    fired = client.post(
        f"/api/harness/schedules/{victim_schedule.id}/fire?runner_id={rid}",
        {"slot": SLOT.isoformat()},
        content_type="application/json",
    )
    assert fired.status_code == 201, fired.content
    assert Turn.objects.get().origin == Turn.ORIGIN_CANOPY_SCHEDULER
