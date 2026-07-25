"""Readiness drills — pinned doctor turns, report callback, per-pair outcomes.

See docs/superpowers/specs/2026-07-24-directed-runner-routing-design.md (Task 7).
"""
from __future__ import annotations

import pytest
from django.test import Client
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


def test_list_runners_includes_drill_rollup(django_user_model):
    """After a drill fan-out + one report, GET /api/harness/runners/ carries the
    rollup RunnerOut.resolve_drill_rollup computes — right counts, right
    last_finished_at — not just the bare RunnerDrill rows list_runner_drills
    already exposed."""
    u = django_user_model.objects.create_user(username="o", password="x")
    client = Client()
    client.force_login(u)
    r = Runner.objects.create(name="s", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                              last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    a1, a2 = (Agent.objects.create(slug=s, name=s) for s in ("echo", "ada"))
    for i, a in enumerate((a1, a2)):
        RunnerAssignment.objects.create(agent=a, runner=r, rank=i)
    d1, d2 = services.start_drill(r, [a1, a2])
    services.report_drill(d1, outcome="pass", summary="all checks green")

    resp = client.get("/api/harness/runners/")
    assert resp.status_code == 200
    [row] = resp.json()
    rollup = row["drill_rollup"]
    assert rollup is not None
    assert rollup["passed"] == 1
    assert rollup["failed"] == 0
    assert rollup["pending"] == 1
    d1.refresh_from_db()
    assert rollup["last_finished_at"] is not None
    assert rollup["last_finished_at"].startswith(d1.finished_at.isoformat()[:19])


def test_list_runners_drill_rollup_none_when_never_drilled(django_user_model):
    u = django_user_model.objects.create_user(username="o2", password="x")
    client = Client()
    client.force_login(u)
    Runner.objects.create(name="fresh", kind=Runner.EMDASH, capabilities={}, paired_by=u,
                          last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    resp = client.get("/api/harness/runners/")
    assert resp.status_code == 200
    [row] = resp.json()
    assert row["drill_rollup"] is None
