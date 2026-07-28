"""Claim routing keyed on Turn.origin (spec 2026-07-27).

Tenancy here is deliberately real rather than stubbed: runner_tenant_slugs derives
from runner.paired_by, and a runner whose pairer is not in the agent's workspace
claims nothing regardless of any rule.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    now = timezone.now()
    laptop = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    cloud = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    return {"user": jj, "ws": ws, "agent": echo, "laptop": laptop, "cloud": cloud}


def _turn(agent, origin, key, *, age_seconds=0):
    turn = Turn.objects.create(
        agent=agent, origin=origin, idempotency_key=key, routing=Turn.ANY
    )
    if age_seconds:
        Turn.objects.filter(pk=turn.pk).update(
            created_at=timezone.now() - dt.timedelta(seconds=age_seconds)
        )
        turn.refresh_from_db()
    return turn


def test_a_source_rule_sends_its_work_to_the_priority_runner(fleet):
    """ace_web work goes to the cloud box even though the laptop is rank 0."""
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=1)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB)

    _turn(a, Turn.ORIGIN_ACE_WEB, "k-ace")

    assert services.claim_next_turn(laptop) is None
    claimed = services.claim_next_turn(cloud)
    assert claimed is not None and claimed.origin == Turn.ORIGIN_ACE_WEB


def test_other_sources_still_follow_the_default_order(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=1)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB)

    _turn(a, Turn.ORIGIN_EMAIL, "k-mail")

    assert services.claim_next_turn(cloud) is None
    assert services.claim_next_turn(laptop) is not None


def test_a_non_strict_rule_degrades_when_its_runner_is_gone(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB)
    Runner.objects.filter(pk=cloud.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1)
    )

    _turn(a, Turn.ORIGIN_ACE_WEB, "k-ace")

    assert services.claim_next_turn(laptop) is not None


def test_a_strict_rule_parks_the_turn_rather_than_degrading(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL, strict=True
    )
    Runner.objects.filter(pk=laptop.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1)
    )

    turn = _turn(a, Turn.ORIGIN_EMAIL, "k-mail")

    assert services.claim_next_turn(cloud) is None
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


def test_strictness_survives_the_cascade_grace(fleet):
    """A turn queued past CASCADE_GRACE_SECONDS opens to lower ranks — but a strict
    rule has no lower ranks, so there is nobody for the grace to promote."""
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL, strict=True
    )
    Runner.objects.filter(pk=laptop.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1)
    )

    _turn(a, Turn.ORIGIN_EMAIL, "k-mail", age_seconds=services.CASCADE_GRACE_SECONDS + 30)

    assert services.claim_next_turn(cloud) is None


def test_a_pin_beats_a_contradicting_rule(fleet):
    """Precedence: explicit runner > source rule."""
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB, strict=True
    )
    Turn.objects.create(
        agent=a, origin=Turn.ORIGIN_ACE_WEB, idempotency_key="k-pin",
        pinned_runner=laptop, routing=Turn.ANY,
    )

    assert services.claim_next_turn(laptop) is not None


def test_a_runner_named_only_by_a_rule_can_claim_that_source(fleet):
    """The cloud box is in no default list at all — the rule alone routes to it."""
    a, cloud = fleet["agent"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=fleet["laptop"], rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB)

    _turn(a, Turn.ORIGIN_ACE_WEB, "k-ace")

    assert services.claim_next_turn(cloud) is not None


def test_a_rule_never_crosses_the_tenant_boundary(fleet):
    """An outsider's runner named by a rule still claims nothing: the rule composes
    the list, the workspace gate decides who may act at all."""
    a = fleet["agent"]
    outsider = get_user_model().objects.create_user(username="mal", email="mal@evil.com")
    theirs = Runner.objects.create(
        name="mal-box", kind=Runner.CLOUD, paired_by=outsider, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={},
    )
    RunnerAssignment.objects.create(agent=a, runner=theirs, rank=0, source=Turn.ORIGIN_ACE_WEB)

    _turn(a, Turn.ORIGIN_ACE_WEB, "k-ace")

    assert services.claim_next_turn(theirs) is None
