"""Claim/lease/idempotency semantics for the harness services."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db


def _agent(slug="echo"):
    # Homed: Agent.workspace is NOT NULL (agents/0013). This helper used to make
    # a workspace-less agent, and claim routing carried an
    # `agent__workspace_id__isnull=True` leg partly to keep this suite green —
    # a fail-open tenancy rule held in place by a fixture.
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=a_workspace())


def _runner(agent=None, **kw):
    """agent: when given, this runner is assigned rank 0 for that agent —
    agent turns route by RunnerAssignment now (spec 2026-07-24), not
    capabilities, so tests that need a claim to succeed must assign."""
    # paired_by is REQUIRED for a claim: a runner's tenant is the workspaces of
    # the human who paired it, and NULL fails closed (services.runner_tenant_slugs).
    defaults = dict(
        name="jj-mbp", kind=Runner.EMDASH, capabilities={"agents": ["echo"]},
        paired_by=a_member(),
    )
    defaults.update(kw)
    r = Runner.objects.create(**defaults)
    services.heartbeat(r, active_turn_ids=[])
    if agent is not None:
        RunnerAssignment.objects.create(agent=agent, runner=r, rank=0)
    return r


def test_enqueue_is_idempotent():
    a = _agent()
    t1, created1 = services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    t2, created2 = services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    assert created1 is True and created2 is False and t1.pk == t2.pk


def test_enqueue_second_key_queues_behind():
    a = _agent()
    services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    t2, created = services.enqueue_turn(agent=a, origin="slack", idempotency_key="k2")
    assert created is True and t2.status == Turn.QUEUED
    assert Turn.objects.filter(agent=a).count() == 2


def test_claim_serializes_execution_per_agent():
    a = _agent()
    services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    services.enqueue_turn(agent=a, origin="slack", idempotency_key="k2")
    r = _runner(a)
    first = services.claim_next_turn(r)
    assert first is not None
    # second queued turn must NOT be claimed while the first is executing
    assert services.claim_next_turn(r) is None
    services.finish_turn(first, status="done")
    second = services.claim_next_turn(r)
    assert second is not None and second.idempotency_key == "k2"


def test_claim_excludes_paused_agents():
    """Per-agent pause: a paused agent's queued turn is not claimed, but stays QUEUED
    (resumable), and other agents are unaffected."""
    a = _agent()
    t, _ = services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    r = _runner(a)
    # echo paused → nothing to claim, and the turn is untouched
    assert services.claim_next_turn(r, exclude_slugs=["echo"]) is None
    t.refresh_from_db()
    assert t.status == Turn.QUEUED
    # excluding an unrelated agent doesn't block echo
    assert services.claim_next_turn(r, exclude_slugs=["hal"]).pk == t.pk


def test_claim_next_turn_happy_path():
    a = _agent()
    t, _ = services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    r = _runner(a)
    claimed = services.claim_next_turn(r)
    assert claimed.pk == t.pk
    claimed.refresh_from_db()
    assert claimed.status == Turn.CLAIMED
    assert claimed.claimed_by_id == r.id
    assert claimed.lease_expires_at > timezone.now()


def test_claim_requires_assignment():
    """Agent turns route by RunnerAssignment (spec 2026-07-24), not capabilities.
    A runner may declare the agent in capabilities but will claim nothing without
    an explicit RunnerAssignment row."""
    a = _agent("eva")
    t, _ = services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    # Runner has eva in capabilities but no RunnerAssignment
    r = _runner(agent=None, capabilities={"agents": ["eva"]})
    assert services.claim_next_turn(r) is None

    # After adding a RunnerAssignment, the claim succeeds
    RunnerAssignment.objects.create(agent=a, runner=r, rank=0)
    claimed = services.claim_next_turn(r)
    assert claimed is not None and claimed.pk == t.pk


def test_local_only_never_claimed_by_cloud():
    a = _agent()
    services.enqueue_turn(agent=a, origin="board", idempotency_key="k1", routing="local_only")
    r = _runner(kind=Runner.CLOUD)
    assert services.claim_next_turn(r) is None


def test_claim_is_exclusive():
    a = _agent()
    services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    r1, r2 = _runner(a), _runner(a, name="jj-mbp-2")
    first = services.claim_next_turn(r1)
    second = services.claim_next_turn(r2)
    assert first is not None and second is None


def test_expired_lease_goes_lost_and_is_reclaimable():
    a = _agent()
    services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    r = _runner(a)
    t = services.claim_next_turn(r)
    Turn.objects.filter(pk=t.pk).update(lease_expires_at=timezone.now() - dt.timedelta(minutes=1))
    assert services.sweep_expired_leases() == 1
    t.refresh_from_db()
    assert t.status == Turn.LOST
    # lost is terminal -> lane free -> a re-enqueue with a new key claims fine
    services.enqueue_turn(agent=a, origin="board", idempotency_key="k2")
    assert services.claim_next_turn(r) is not None


def test_heartbeat_renews_lease_and_status():
    a = _agent()
    services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    r = _runner(a)
    t = services.claim_next_turn(r)
    old_expiry = t.lease_expires_at
    services.heartbeat(r, active_turn_ids=[str(t.pk)])
    t.refresh_from_db()
    assert t.lease_expires_at > old_expiry
    r.refresh_from_db()
    assert r.status == Runner.ONLINE


def test_degraded_runner_claims_nothing():
    a = _agent()
    services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    r = _runner()
    services.heartbeat(r, active_turn_ids=[], degraded=True, note="emdash schema drift")
    assert services.claim_next_turn(r) is None


def test_append_events_assigns_monotonic_seq():
    a = _agent()
    t, _ = services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    n = services.append_events(t, [{"kind": "status", "payload": {"s": "claimed"}}])
    n += services.append_events(t, [{"kind": "status", "payload": {"s": "running"}}])
    assert n == 2
    assert list(t.events.values_list("seq", flat=True)) == [1, 2]


def test_finish_turn_sets_terminal_state():
    a = _agent()
    t, _ = services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    r = _runner(a)
    claimed = services.claim_next_turn(r)
    services.finish_turn(claimed, status="done", result_note="2 commands applied")
    t.refresh_from_db()
    assert t.status == Turn.DONE and t.finished_at is not None


def test_finish_turn_does_not_resurrect_lost_turn():
    a = _agent()
    t, _ = services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    r = _runner(a)
    claimed = services.claim_next_turn(r)
    # simulate a lease sweep declaring the turn lost while the runner is
    # still (unknowingly) working on it
    Turn.objects.filter(pk=claimed.pk).update(lease_expires_at=timezone.now() - dt.timedelta(minutes=1))
    services.sweep_expired_leases()
    claimed.refresh_from_db()
    assert claimed.status == Turn.LOST
    events_before = claimed.events.count()

    result = services.finish_turn(claimed, status="done", result_note="zombie write")
    result.refresh_from_db()
    assert result.status == Turn.LOST  # not resurrected to done
    assert result.result_note != "zombie write"
    assert result.events.count() == events_before  # no extra event appended


def test_mark_running_does_not_resurrect_lost_turn():
    a = _agent()
    t, _ = services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    r = _runner(a)
    claimed = services.claim_next_turn(r)
    Turn.objects.filter(pk=claimed.pk).update(lease_expires_at=timezone.now() - dt.timedelta(minutes=1))
    services.sweep_expired_leases()
    claimed.refresh_from_db()
    assert claimed.status == Turn.LOST
    events_before = claimed.events.count()

    result = services.mark_running(claimed, session_id="zombie-session")
    result.refresh_from_db()
    assert result.status == Turn.LOST  # not resurrected to running
    assert result.session_id != "zombie-session"
    assert result.events.count() == events_before  # no extra event appended


def test_finish_turn_rejects_bad_status():
    a = _agent()
    t, _ = services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    with pytest.raises(ValueError):
        services.finish_turn(t, status="queued")


def test_finish_turn_idempotent_on_terminal():
    a = _agent()
    t, _ = services.enqueue_turn(agent=a, origin="board", idempotency_key="k1")
    r = _runner(a)
    claimed = services.claim_next_turn(r)
    services.finish_turn(claimed, status="done", result_note="first")
    t.refresh_from_db()
    events_after_first = t.events.count()

    # a second finish on an already-terminal turn is a no-op: status/note
    # stay as they were and no additional event is appended
    result = services.finish_turn(t, status="failed", result_note="second")
    result.refresh_from_db()
    assert result.status == Turn.DONE
    assert result.result_note == "first"
    assert result.events.count() == events_after_first


# ---------------------------------------------------------------------------
# Sessionless-failure requeue: a turn that died before its session existed is a
# NON-attempt, not a failed attempt. See services.finish_turn.
# ---------------------------------------------------------------------------


def _claimed_turn(origin="email", key="k1", slug="echo"):
    a = _agent(slug)
    services.enqueue_turn(agent=a, origin=origin, idempotency_key=key)
    r = _runner(a)
    return services.claim_next_turn(r), r


def test_failed_turn_with_no_session_goes_back_on_the_queue():
    """The Stripe-email case: emdash never came up, so no agent ever saw the prompt."""
    claimed, _ = _claimed_turn()
    services.mark_running(claimed)

    result = services.finish_turn(
        claimed, status="failed",
        result_note="emdash create failed: cannot connect to emdash CDP on 127.0.0.1:9223",
    )
    result.refresh_from_db()
    assert result.status == Turn.QUEUED
    assert result.attempts == 1
    # the claim is fully released so a HEALTHY runner can take it
    assert result.claimed_by_id is None
    assert result.claimed_at is None and result.lease_expires_at is None
    assert result.started_at is None
    assert result.finished_at is None
    # ...and why it went back is on the ledger, not just inferable
    ev = result.events.order_by("-id").first()
    assert ev.payload["requeued_after"] == Turn.FAILED and ev.payload["attempt"] == 1


def test_requeued_turn_is_claimable_again():
    claimed, r = _claimed_turn()
    services.finish_turn(claimed, status="failed", result_note="emdash create failed: boom")
    again = services.claim_next_turn(r)
    assert again is not None and again.pk == claimed.pk


def test_failed_turn_that_HAD_a_session_stays_terminal():
    """The guard that makes the retry safe: a session existed, so the agent may have
    already sent mail or edited files. Never re-run that."""
    claimed, _ = _claimed_turn()
    Turn.objects.filter(pk=claimed.pk).update(emdash_task_id="eva-api-8195-0904-0711")
    claimed.refresh_from_db()

    result = services.finish_turn(claimed, status="failed", result_note="agent errored mid-turn")
    result.refresh_from_db()
    assert result.status == Turn.FAILED
    assert result.attempts == 0 and result.finished_at is not None


def test_requeue_gives_up_after_the_cap():
    claimed, r = _claimed_turn()
    for expected in range(1, services.MAX_SESSIONLESS_RETRIES + 1):
        t = services.claim_next_turn(r) if expected > 1 else claimed
        services.finish_turn(t, status="failed", result_note="emdash create failed: still down")
        t.refresh_from_db()
        assert t.status == Turn.QUEUED and t.attempts == expected

    # one more failure exhausts the budget and the turn stays dead + visible
    final = services.claim_next_turn(r)
    services.finish_turn(final, status="failed", result_note="emdash create failed: still down")
    final.refresh_from_db()
    assert final.status == Turn.FAILED
    assert final.attempts == services.MAX_SESSIONLESS_RETRIES
    assert final.finished_at is not None


def test_done_and_cancelled_are_never_requeued():
    for status in ("done", "cancelled"):
        claimed, _ = _claimed_turn(key=f"k-{status}", slug=f"echo-{status}")
        services.finish_turn(claimed, status=status, result_note="")
        claimed.refresh_from_db()
        assert claimed.status == status and claimed.attempts == 0


def test_requeue_does_not_resurrect_a_lost_turn():
    """A lease sweep already declared it dead; a late sessionless failure report from
    the zombie runner must not put it back on the queue."""
    claimed, _ = _claimed_turn()
    Turn.objects.filter(pk=claimed.pk).update(
        lease_expires_at=timezone.now() - dt.timedelta(minutes=1))
    services.sweep_expired_leases()
    claimed.refresh_from_db()
    assert claimed.status == Turn.LOST

    result = services.finish_turn(claimed, status="failed", result_note="emdash create failed: late")
    result.refresh_from_db()
    assert result.status == Turn.LOST and result.attempts == 0


def test_a_failed_drill_is_never_requeued():
    """A drill's whole job is to find out whether a session can be created, so a
    sessionless failure is its ANSWER. Requeueing would retry the probe and leave the
    RunnerDrill pending while it churned."""
    from apps.harness.models import RunnerDrill

    a = _agent()
    r = _runner(a)
    Runner.objects.filter(pk=r.pk).update(status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    r.refresh_from_db()
    [drill] = services.start_drill(r, [a])
    turn = drill.turn
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CLAIMED, claimed_by=r)
    turn.refresh_from_db()

    services.finish_turn(turn, status=Turn.FAILED,
                         result_note="emdash create failed: cannot connect to emdash CDP")
    turn.refresh_from_db()
    drill.refresh_from_db()
    assert turn.status == Turn.FAILED and turn.attempts == 0
    assert drill.outcome == RunnerDrill.OUTCOME_FAIL
