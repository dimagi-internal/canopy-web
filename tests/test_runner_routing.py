"""Claim routing — pinned turns (spec 2026-07-24-directed-runner-routing, Task 3).

A `pinned_runner` on a Turn is a HARD pin: only that runner may claim it,
bypassing assignments/capabilities entirely, but never the tenant gate. Mirrors
the fixture idioms already used by the other harness claim tests
(test_harness_authz.py, test_harness_claim_projects.py).
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

User = get_user_model()


def _user(name):
    return User.objects.create_user(username=name, email=f"{name}@dimagi.com")


def _ws(slug, owner):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    return ws


def _runner(name, pairer, **kw):
    defaults = dict(
        name=name, kind=Runner.EMDASH, host=name, paired_by=pairer,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={},
    )
    defaults.update(kw)
    return Runner.objects.create(**defaults)


def test_pinned_turn_invisible_to_other_runners():
    """A turn pinned to r1 is claimable by r1 even though r1 declares no
    capabilities at all, and stays invisible to r2 even though r2 is the
    agent's ranked assignment."""
    u = _user("owner")
    ws = _ws("canopy", u)
    a = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    r1 = _runner("r1", u)
    r2 = _runner("r2", u, capabilities={"agents": ["echo"]})
    RunnerAssignment.objects.create(agent=a, runner=r2, rank=0)

    turn, _ = services.enqueue_turn(
        agent=a, origin=Turn.ORIGIN_DRILL, idempotency_key="p1", pinned_runner=r1,
    )

    assert services.claim_next_turn(r2) is None  # pinned elsewhere -> invisible

    claimed = services.claim_next_turn(r1)
    assert claimed is not None and claimed.pk == turn.pk
    assert claimed.status == Turn.CLAIMED


def test_pin_bypasses_assignments_but_not_tenancy():
    """The security-critical case: a runner paired by a non-member of the
    agent's workspace must NOT be able to claim a pinned turn just because it
    is the pin target. Tenancy gates before the pin ever gets a look."""
    owner = _user("owner")
    stranger = _user("stranger")
    victim_ws = _ws("canopy", owner)
    _ws("stranger-space", stranger)  # tenanted, just not a member of canopy

    a = Agent.objects.create(slug="echo", name="Echo", workspace=victim_ws)
    r_stranger = _runner("evil", stranger)

    turn, _ = services.enqueue_turn(
        agent=a, origin=Turn.ORIGIN_DRILL, idempotency_key="p2", pinned_runner=r_stranger,
    )

    assert services.claim_next_turn(r_stranger) is None  # tenant gate holds even when pinned
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED
    assert turn.claimed_by is None
