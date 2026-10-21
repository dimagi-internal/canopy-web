"""A stuck turn's warning names the real cause and can be closed where it is shown.

Four readiness drills sat six days in the supervisor's "no runner can claim" card
(2026-10-21): each was pinned to cloud-ec2-1, which had been retired. The card said
"no runner routes the agent 'hal'" — sending its owner to fix routing that was fine
— told them to "cancel the turn" with nothing to press, and nothing ever ended them.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import initiator, services
from apps.harness.drills import DRILL_EXPIRY, expire_stale_drills
from apps.harness.models import Runner, RunnerDrill, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet():
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=jj)
    WorkspaceMembership.objects.create(user=jj, workspace=ws, role=WorkspaceMembership.OWNER)
    viewer = User.objects.create_user("v", "v@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=ws, role=WorkspaceMembership.VIEWER)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=jj)
    box = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, workspace=ws, owner=jj,
                                status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                capabilities={"agents": ["hal"]})
    return {"jj": jj, "viewer": viewer, "ws": ws, "hal": hal, "box": box}


def _age(turn, by):
    Turn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - by)


def _stuck(user):
    c = Client()
    c.force_login(user)
    res = c.get("/api/harness/turns/unclaimable")
    assert res.status_code == 200, res.content
    return res.json()


def _drill(f):
    [drill] = services.start_drill(f["box"], [f["hal"]])
    _age(drill.turn, services.UNCLAIMABLE_GRACE + dt.timedelta(seconds=5))
    return drill


def test_a_turn_pinned_to_a_retired_runner_says_so(fleet):
    _drill(fleet)
    Runner.objects.filter(pk=fleet["box"].pk).update(status=Runner.RETIRED)
    [row] = _stuck(fleet["jj"])
    assert row["reason"] == "pinned to cloud-ec2-1, which has been retired — it will never run"
    assert row["kind"] == "config"


def test_a_turn_pinned_to_a_paused_runner_names_that_runner(fleet):
    _drill(fleet)
    Runner.objects.filter(pk=fleet["box"].pk).update(paused=True, paused_at=timezone.now())
    [row] = _stuck(fleet["jj"])
    assert row["kind"] == "parked"
    assert row["reason"].startswith("pinned to one runner: cloud-ec2-1 was paused")


def test_each_row_says_who_may_cancel_it(fleet):
    _drill(fleet)
    Runner.objects.filter(pk=fleet["box"].pk).update(status=Runner.RETIRED)
    assert _stuck(fleet["jj"])[0]["can_cancel"] is True
    assert all(not r["can_cancel"] for r in _stuck(fleet["viewer"]))


def test_retiring_a_runner_cancels_what_is_pinned_to_it(fleet):
    drill = _drill(fleet)
    c = Client()
    c.force_login(fleet["jj"])
    assert c.post(f"/api/harness/runners/{fleet['box'].pk}/retire").status_code == 200
    drill.refresh_from_db()
    drill.turn.refresh_from_db()
    assert drill.turn.status == Turn.CANCELLED
    assert "cloud-ec2-1, the runner it was pinned to, was retired" in drill.turn.result_note
    assert drill.outcome == RunnerDrill.OUTCOME_FAIL


def test_a_drill_that_waits_a_day_expires_with_the_runners_situation(fleet):
    stale = _drill(fleet)
    _age(stale.turn, DRILL_EXPIRY + dt.timedelta(minutes=1))
    other = Agent.objects.create(slug="eva", name="Eva", workspace=fleet["ws"], owner=fleet["jj"])
    [fresh] = services.start_drill(fleet["box"], [other])
    Runner.objects.filter(pk=fleet["box"].pk).update(paused=True, paused_at=timezone.now())
    assert expire_stale_drills() == 1
    stale.refresh_from_db()
    stale.turn.refresh_from_db()
    assert stale.turn.status == Turn.CANCELLED
    assert stale.outcome == RunnerDrill.OUTCOME_FAIL
    assert stale.summary.startswith("expired, never ran within a day: cloud-ec2-1 was paused")
    fresh.turn.refresh_from_db()
    assert fresh.turn.status == Turn.QUEUED, fresh.turn.result_note


def test_an_ordinary_pinned_turn_never_expires(fleet):
    t, _ = services.enqueue_turn(initiator=initiator.system(via="test"), agent=fleet["hal"], origin=Turn.ORIGIN_API, idempotency_key="k1",
                                 prompt="go", pinned_runner=fleet["box"])
    _age(t, DRILL_EXPIRY * 3)
    assert expire_stale_drills() == 0
    t.refresh_from_db()
    assert t.status == Turn.QUEUED


def test_a_re_drill_replaces_the_one_still_waiting(fleet):
    first = _drill(fleet)
    first_turn = first.turn
    services.start_drill(fleet["box"], [fleet["hal"]])
    first_turn.refresh_from_db()
    assert first_turn.status == Turn.CANCELLED
    assert Turn.objects.filter(status=Turn.QUEUED, pinned_runner=fleet["box"]).count() == 1
