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


_user_seq = 0


def _user(name=None):
    global _user_seq
    if name is None:
        _user_seq += 1
        name = f"user{_user_seq}"
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


# Alias used by the assignment-cascade tests below (Task 4) — same shape as
# _runner, named for readability at the call site (an "available" runner).
_online_runner = _runner


def _agent(slug="echo", workspace=None):
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=workspace)


def _assign(agent, runner, rank, enabled=True):
    return RunnerAssignment.objects.create(agent=agent, runner=runner, rank=rank, enabled=enabled)


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


# --- assignment cascade (Task 4) --------------------------------------------


def test_rank0_available_blocks_rank1():
    u = _user()
    r0, r1 = _online_runner("r0", u), _online_runner("r1", u)
    a = _agent("echo")
    _assign(a, r0, 0); _assign(a, r1, 1)
    services.enqueue_turn(agent=a, origin=Turn.ORIGIN_API, idempotency_key="c1")
    assert services.claim_next_turn(r1) is None      # r0 is available → r1 waits
    assert services.claim_next_turn(r0) is not None  # r0 claims


def test_rank1_takes_over_when_rank0_offline():
    u = _user()
    r0 = Runner.objects.create(name="r0", kind=Runner.EMDASH, capabilities={}, paired_by=u)  # never heartbeat
    r1 = _online_runner("r1", u)
    a = _agent("echo")
    _assign(a, r0, 0); _assign(a, r1, 1)
    services.enqueue_turn(agent=a, origin=Turn.ORIGIN_API, idempotency_key="c2")
    assert services.claim_next_turn(r1) is not None


def test_not_ready_counts_as_unavailable():
    u = _user()
    r0 = _online_runner("r0", u); r0.ready = False; r0.save()
    r1 = _online_runner("r1", u)
    a = _agent("echo")
    _assign(a, r0, 0); _assign(a, r1, 1)
    services.enqueue_turn(agent=a, origin=Turn.ORIGIN_API, idempotency_key="c3")
    assert services.claim_next_turn(r1) is not None


def test_grace_opens_next_rank_even_when_rank0_available(monkeypatch):
    u = _user()
    r0, r1 = _online_runner("r0", u), _online_runner("r1", u)
    a = _agent("echo")
    _assign(a, r0, 0); _assign(a, r1, 1)
    turn, _ = services.enqueue_turn(agent=a, origin=Turn.ORIGIN_API, idempotency_key="c4")
    Turn.objects.filter(pk=turn.pk).update(
        created_at=timezone.now() - timezone.timedelta(seconds=services.CASCADE_GRACE_SECONDS + 1)
    )
    assert services.claim_next_turn(r1) is not None  # r0 online-but-stuck can't wedge the queue


def test_unassigned_runner_never_claims_even_with_capabilities():
    u = _user()
    r = _online_runner("r", u, capabilities={"agents": ["echo"]})
    a = _agent("echo")  # no assignments at all → unroutable
    services.enqueue_turn(agent=a, origin=Turn.ORIGIN_API, idempotency_key="c5")
    assert services.claim_next_turn(r) is None


# --- enabled toggle (operator follow-up: disable, don't remove) -------------


def test_disabled_rank0_does_not_block_enabled_rank1():
    """A disabled rank-0 row must neither claim nor count as a better-ranked
    availability blocker — rank 1 claims immediately, no grace wait needed."""
    u = _user()
    r0, r1 = _online_runner("r0", u), _online_runner("r1", u)
    a = _agent("echo")
    _assign(a, r0, 0, enabled=False)
    _assign(a, r1, 1)
    services.enqueue_turn(agent=a, origin=Turn.ORIGIN_API, idempotency_key="c6")
    assert services.claim_next_turn(r0) is None      # disabled → never claims
    claimed = services.claim_next_turn(r1)
    assert claimed is not None and claimed.status == Turn.CLAIMED


def test_disabled_runner_itself_never_claims():
    u = _user()
    r0 = _online_runner("r0", u)
    a = _agent("echo")
    _assign(a, r0, 0, enabled=False)
    services.enqueue_turn(agent=a, origin=Turn.ORIGIN_API, idempotency_key="c7")
    assert services.claim_next_turn(r0) is None


# --- session-binding stickiness (Task 5) ------------------------------------


def _session(agent=None, workspace=None, project=""):
    from apps.canopy_sessions.models import Session
    return Session.objects.create(agent=agent, workspace=workspace, project=project)


def _bind(session, runner):
    from apps.canopy_sessions.models import RunnerBinding
    return RunnerBinding.objects.create(session=session, runner=runner, thread_key=str(session.id))


def test_bound_session_claims_only_on_binding_holder():
    u = _user()
    ws = _ws("w1", u)
    holder = _online_runner("holder", u, capabilities={"sessions": True})
    other = _online_runner("other", u, capabilities={"sessions": True})
    s = _session(workspace=ws, project="canopy-web")
    _bind(s, holder)
    services.enqueue_turn(session=s, origin=Turn.ORIGIN_API, idempotency_key="s1")
    assert services.claim_next_turn(other) is None
    assert services.claim_next_turn(holder) is not None


def test_bound_session_with_offline_holder_waits_for_placement():
    u = _user()
    ws = _ws("w1", u)
    holder = Runner.objects.create(name="gone", kind=Runner.EMDASH,
                                   capabilities={"sessions": True}, paired_by=u)
    other = _online_runner("other", u, capabilities={"sessions": True})
    s = _session(workspace=ws, project="canopy-web")
    _bind(s, holder)
    services.enqueue_turn(session=s, origin=Turn.ORIGIN_API, idempotency_key="s2")
    assert services.claim_next_turn(other) is None  # nobody claims until user places


def test_unbound_agent_session_follows_assignment_order():
    u = _user()
    ws = _ws("w1", u)
    a = _agent("echo", workspace=ws)
    r0 = Runner.objects.create(name="r0", kind=Runner.EMDASH, capabilities={"sessions": True}, paired_by=u)
    r1 = _online_runner("r1", u, capabilities={"sessions": True})
    _assign(a, r0, 0); _assign(a, r1, 1)
    s = _session(agent=a, workspace=ws)
    services.enqueue_turn(session=s, origin=Turn.ORIGIN_API, idempotency_key="s3")
    assert services.claim_next_turn(r1) is not None  # r0 offline → r1 (rank 1) takes it
