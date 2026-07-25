"""Readiness drills — pinned doctor turns, report callback, per-pair outcomes.

See docs/superpowers/specs/2026-07-24-directed-runner-routing-design.md (Task 7).
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, RunnerDrill, Turn

pytestmark = pytest.mark.django_db


def test_start_drill_fans_out_pinned_pending_turns(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    r = Runner.objects.create(name="standby", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a1, a2 = (Agent.objects.create(slug=s, name=s) for s in ("echo", "ada"))
    for i, a in enumerate((a1, a2)):
        RunnerAssignment.objects.create(agent=a, runner=r, rank=i)
    drills = services.start_drill(r, [a1, a2])
    assert {d.outcome for d in drills} == {RunnerDrill.OUTCOME_PENDING}
    turns = Turn.objects.filter(origin=Turn.ORIGIN_DRILL)
    assert turns.count() == 2
    assert all(t.pinned_runner_id == r.id for t in turns)
    assert "read-only" in turns.first().prompt.lower()


def test_report_drill_resolves_outcome(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    r = Runner.objects.create(name="s", kind=Runner.EMDASH, capabilities={}, paired_by=u)
    a = Agent.objects.create(slug="echo", name="Echo")
    d = RunnerDrill.objects.create(runner=r, agent=a)
    services.report_drill(d, outcome="pass", summary="all checks green")
    d.refresh_from_db()
    assert d.outcome == RunnerDrill.OUTCOME_PASS and d.finished_at is not None


def test_failed_drill_turn_marks_drill_fail(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    r = Runner.objects.create(name="s", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a = Agent.objects.create(slug="echo", name="Echo")
    RunnerAssignment.objects.create(agent=a, runner=r, rank=0)
    [d] = services.start_drill(r, [a])
    turn = Turn.objects.get(origin=Turn.ORIGIN_DRILL)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CLAIMED, claimed_by=r)
    turn.refresh_from_db()
    services.finish_turn(turn, status=Turn.FAILED, result_note="claude auth expired")
    d.refresh_from_db()
    assert d.outcome == RunnerDrill.OUTCOME_FAIL
    assert "claude auth expired" in d.summary


def test_redrill_resets_to_pending(django_user_model):
    u = django_user_model.objects.create_user(username="o", password="x")
    r = Runner.objects.create(name="s", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a = Agent.objects.create(slug="echo", name="Echo")
    RunnerAssignment.objects.create(agent=a, runner=r, rank=0)
    [d] = services.start_drill(r, [a])
    services.report_drill(d, outcome="pass", summary="ok")
    [d2] = services.start_drill(r, [a])
    assert d2.pk == d.pk and d2.outcome == RunnerDrill.OUTCOME_PENDING
