import pytest
from django.db import IntegrityError

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAssignment, RunnerDrill, Turn

pytestmark = pytest.mark.django_db


def _agent(slug="echo"):
    return Agent.objects.create(slug=slug, name=slug.title())


def _runner(name="r1", capabilities=None, **kw):
    return Runner.objects.create(name=name, kind=Runner.EMDASH, capabilities=capabilities or {}, **kw)


def test_assignment_orders_by_rank():
    a, r1, r2 = _agent(), _runner("r1"), _runner("r2")
    RunnerAssignment.objects.create(agent=a, runner=r2, rank=1)
    RunnerAssignment.objects.create(agent=a, runner=r1, rank=0)
    assert [x.runner for x in a.runner_assignments.all()] == [r1, r2]


def test_assignment_unique_per_agent_runner():
    a, r1 = _agent(), _runner()
    RunnerAssignment.objects.create(agent=a, runner=r1, rank=0)
    with pytest.raises(IntegrityError):
        RunnerAssignment.objects.create(agent=a, runner=r1, rank=1)


def test_drill_unique_per_pair_and_defaults_pending():
    a, r1 = _agent(), _runner()
    d = RunnerDrill.objects.create(runner=r1, agent=a)
    assert d.outcome == RunnerDrill.OUTCOME_PENDING
    with pytest.raises(IntegrityError):
        RunnerDrill.objects.create(runner=r1, agent=a)


def test_turn_pinned_runner_nullable_and_origin_drill():
    a, r1 = _agent(), _runner()
    t = Turn.objects.create(
        agent=a, origin=Turn.ORIGIN_DRILL, idempotency_key="k1", pinned_runner=r1
    )
    r1.delete()
    t.refresh_from_db()
    assert t.pinned_runner is None  # SET_NULL degrades pin to normal routing


def test_seed_assignments_from_capabilities():
    from apps.harness.services import seed_assignments_from_capabilities

    echo = _agent("echo")
    echo.runner_preference = ["cloud", "emdash"]
    echo.save()
    ada = _agent("ada")
    laptop = _runner("laptop", capabilities={"agents": ["echo", "ada"]})
    cloud = Runner.objects.create(name="cloudy", kind=Runner.CLOUD, capabilities={"agents": ["echo"]})
    retired = Runner.objects.create(
        name="old", kind=Runner.EMDASH, status=Runner.RETIRED, capabilities={"agents": ["echo"]}
    )
    assert seed_assignments_from_capabilities() == 3
    assert [x.runner for x in echo.runner_assignments.all()] == [cloud, laptop]  # cloud kind preferred
    assert [x.runner for x in ada.runner_assignments.all()] == [laptop]
    assert seed_assignments_from_capabilities() == 0  # idempotent
