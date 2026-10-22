"""The claim never hands a box a turn of an agent the box REPORTS it cannot run.

fizzy, 2026-10-15: an email turn was claimed by cloud-ec2-2, which had no clone of
fizzy (so neither its plugin nor its credentials), ran in a bare scratch dir and
ended having done nothing; another fizzy turn the same day was claimed by
shayoni-win-cdp, whose emdash had no `fizzy` project, and failed at once. Both
boxes passed every gate the claim asked (tenant, hold, routing). Now a box's own
report is a FLOOR on the routed list (`routing.cannot_serve`): it neither claims
nor blocks a lower rank, and a turn only such boxes would take is reported as
stuck, with what each box said.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import routing
from apps.harness import claim
from apps.harness.models import Runner, RunnerAssignment, Turn, WorkspaceRunnerOrder
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _runner(owner, name, ws, kind=Runner.CLOUD, caps=None):
    return Runner.objects.create(name=name, kind=kind, host=name, owner=owner, workspace=ws,
                                 status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                 capabilities=caps or {})


@pytest.fixture
def fleet():
    jj, shayoni = _user("jj"), _user("shayoni")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    strategy = Workspace.objects.create(slug="strategy", display_name="Strategy",
                                        created_by=jj, parent=dimagi)
    WorkspaceMembership.objects.create(workspace=dimagi, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=strategy, user=shayoni,
                                       role=WorkspaceMembership.OWNER)
    cloud = _runner(jj, "cloud-ec2-2", dimagi)  # enrolled in dimagi: covers strategy
    laptop = _runner(shayoni, "shayoni-win-cdp", strategy, kind=Runner.EMDASH,
                     caps={"projects": ["fizzy"]})
    fizzy = Agent.objects.create(slug="fizzy", name="fizzy", workspace=strategy, owner=shayoni)
    return {"jj": jj, "cloud": cloud, "laptop": laptop, "fizzy": fizzy, "dimagi": dimagi}


def _turn(agent, key="t", origin=Turn.ORIGIN_EMAIL, age=3600):
    t = Turn.objects.create(agent=agent, origin=origin, idempotency_key=key,
                            routing=Turn.PREFER_LOCAL, prompt="Test: reply with today's date")
    Turn.objects.filter(pk=t.pk).update(created_at=timezone.now() - dt.timedelta(seconds=age))
    return t


def _beat(runner, **body):
    c = Client()
    c.force_login(runner.owner)
    r = c.post(f"/api/harness/runners/{runner.pk}/heartbeat",
               data={"active_turn_ids": [], **body}, content_type="application/json")
    assert r.status_code == 200, r.content
    runner.refresh_from_db()


def _offline(runner):
    Runner.objects.filter(pk=runner.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1))
    runner.refresh_from_db()


def test_the_incident_shape_claims_today_without_a_report(fleet):
    """The gap, stated: jj owns Dimagi, so his cloud box may hold fizzy, and
    Dimagi's order lists it — with Shayoni's laptop offline it takes the turn."""
    WorkspaceRunnerOrder.objects.create(workspace=fleet["dimagi"], runner=fleet["cloud"], rank=0)
    t = _turn(fleet["fizzy"])
    assert claim.claim_next_turn(fleet["cloud"]).pk == t.pk


def test_a_cloud_box_that_could_not_set_the_agent_up_does_not_claim_it(fleet):
    cloud, fizzy = fleet["cloud"], fleet["fizzy"]
    WorkspaceRunnerOrder.objects.create(workspace=fleet["dimagi"], runner=cloud, rank=0)
    _beat(cloud, agents_unprepared=["fizzy"])
    assert cloud.capabilities["unprepared"] == ["fizzy"]
    t = _turn(fizzy)
    assert claim.claim_next_turn(cloud) is None
    assert Turn.objects.get(pk=t.pk).status == Turn.QUEUED
    # The box still RUNS fizzy (its roster keeps it, so the next bootstrap retries).
    assert routing.runner_routes_agent(cloud, fizzy)
    # Set up again: the next beat says so, and the claim takes it.
    _beat(cloud, agents_unprepared=[])
    assert claim.claim_next_turn(cloud).pk == t.pk


def test_an_absent_report_leaves_the_stored_one_alone(fleet):
    cloud = fleet["cloud"]
    _beat(cloud, agents_unprepared=["fizzy"])
    _beat(cloud)  # an older box, or a beat that does not build the list
    assert cloud.capabilities["unprepared"] == ["fizzy"]


def test_a_laptop_without_the_agents_emdash_project_does_not_claim_it(fleet):
    laptop, fizzy = fleet["laptop"], fleet["fizzy"]
    RunnerAssignment.objects.create(agent=fizzy, runner=laptop, rank=0)
    Runner.objects.filter(pk=laptop.pk).update(capabilities={"projects": ["other"]})
    laptop.refresh_from_db()
    t = _turn(fizzy, origin=Turn.ORIGIN_API)
    assert claim.claim_next_turn(laptop) is None
    Runner.objects.filter(pk=laptop.pk).update(capabilities={"projects": ["fizzy"]})
    laptop.refresh_from_db()
    assert claim.claim_next_turn(laptop).pk == t.pk


def test_a_laptop_that_never_reported_projects_is_unknown_not_empty(fleet):
    laptop, fizzy = fleet["laptop"], fleet["fizzy"]
    RunnerAssignment.objects.create(agent=fizzy, runner=laptop, rank=0)
    Runner.objects.filter(pk=laptop.pk).update(capabilities={})
    laptop.refresh_from_db()
    t = _turn(fizzy, origin=Turn.ORIGIN_API)
    assert claim.claim_next_turn(laptop).pk == t.pk


def test_an_unservable_box_does_not_block_a_lower_rank(fleet):
    """A floor, not a skip: the box that cannot run it is out of the list, so the
    next box takes the turn at once rather than after the cascade grace."""
    cloud, laptop, fizzy = fleet["cloud"], fleet["laptop"], fleet["fizzy"]
    RunnerAssignment.objects.create(agent=fizzy, runner=cloud, rank=0)
    RunnerAssignment.objects.create(agent=fizzy, runner=laptop, rank=1)
    _beat(cloud, agents_unprepared=["fizzy"])
    t = _turn(fizzy, origin=Turn.ORIGIN_API, age=0)
    assert claim.claim_next_turn(laptop).pk == t.pk


def test_a_strict_rule_whose_box_cannot_run_it_waits_rather_than_falls_through(fleet):
    cloud, laptop, fizzy = fleet["cloud"], fleet["laptop"], fleet["fizzy"]
    RunnerAssignment.objects.create(agent=fizzy, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=fizzy, runner=cloud, rank=0,
                                    source=Turn.ORIGIN_EMAIL, strict=True)
    _beat(cloud, agents_unprepared=["fizzy"])
    _turn(fizzy)
    assert claim.claim_next_turn(laptop) is None
    assert claim.claim_next_turn(cloud) is None


def test_a_pin_is_a_placement_and_still_lands(fleet):
    """A drill pins a box to find out whether it can run the agent; refusing it
    would leave the drill queued instead of reporting the failure."""
    cloud, fizzy = fleet["cloud"], fleet["fizzy"]
    _beat(cloud, agents_unprepared=["fizzy"])
    t = _turn(fizzy, origin=Turn.ORIGIN_API)
    Turn.objects.filter(pk=t.pk).update(pinned_runner=cloud)
    assert claim.claim_next_turn(cloud).pk == t.pk


def test_a_turn_only_unservable_boxes_would_take_is_reported_with_what_they_said(fleet):
    cloud, laptop, fizzy = fleet["cloud"], fleet["laptop"], fleet["fizzy"]
    WorkspaceRunnerOrder.objects.create(workspace=fleet["dimagi"], runner=cloud, rank=0)
    _offline(laptop)
    _beat(cloud, agents_unprepared=["fizzy"])
    t = _turn(fizzy)
    rows = {r["turn_id"]: r for r in claim.unclaimable_queued_turns(fleet["jj"])}
    row = rows[str(t.pk)]
    assert row["kind"] == "config"
    assert "cloud-ec2-2 could not set 'fizzy' up" in row["reason"]


def test_a_chat_with_the_agent_is_held_to_the_same_report(fleet):
    from apps.canopy_sessions.models import Session

    cloud, fizzy = fleet["cloud"], fleet["fizzy"]
    RunnerAssignment.objects.create(agent=fizzy, runner=cloud, rank=0)
    Runner.objects.filter(pk=cloud.pk).update(capabilities={"sessions": True})
    cloud.refresh_from_db()
    _beat(cloud, agents_unprepared=["fizzy"])
    sess = Session.objects.create(agent=fizzy, workspace_id="strategy", created_by=fleet["jj"])
    t = Turn.objects.create(chat_session=sess, origin=Turn.ORIGIN_API, idempotency_key="c",
                            routing=Turn.ANY, prompt="hi")
    assert claim.claim_next_turn(cloud) is None
    _beat(cloud, agents_unprepared=[])
    assert claim.claim_next_turn(cloud).pk == t.pk


def test_unprepared_is_reported_never_set_by_hand(fleet):
    cloud = fleet["cloud"]
    c = Client()
    c.force_login(cloud.owner)
    r = c.patch(f"/api/harness/runners/{cloud.pk}",
              data={"capabilities": {"unprepared": []}}, content_type="application/json")
    assert r.status_code == 422
