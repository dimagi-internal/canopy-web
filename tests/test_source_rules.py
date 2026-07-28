"""Source rules: one priority runner per (agent, source), plus a strict toggle.

The composition helper is pure — it takes the loaded rows and returns the ordered
list the existing cascade walks. Everything about ranks, availability and the
grace stays in claim_next_turn; this only decides WHICH list gets cascaded.
"""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


def _agent(slug="echo"):
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=a_workspace())


def _runner(name):
    return Runner.objects.create(name=name, kind=Runner.EMDASH, capabilities={})


def _rows(agent, origin):
    defaults, priorities = services.load_assignment_rows([agent.id])
    return [r for _rank, r in services.assignment_rows_for(agent.id, origin, defaults, priorities)]


def test_one_priority_runner_per_agent_and_source():
    a, r1, r2 = _agent(), _runner("r1"), _runner("r2")
    RunnerAssignment.objects.create(agent=a, runner=r1, rank=0, source=Turn.ORIGIN_EMAIL)
    with pytest.raises(IntegrityError):
        RunnerAssignment.objects.create(agent=a, runner=r2, rank=0, source=Turn.ORIGIN_EMAIL)


def test_the_same_runner_may_be_a_default_and_a_priority():
    """A runner is normally BOTH: rank 2 by default, first for email."""
    a, r1 = _agent(), _runner("r1")
    RunnerAssignment.objects.create(agent=a, runner=r1, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=r1, rank=0, source=Turn.ORIGIN_EMAIL)
    assert RunnerAssignment.objects.filter(agent=a).count() == 2


def test_no_rule_returns_the_default_list_in_rank_order():
    a, r1, r2 = _agent(), _runner("r1"), _runner("r2")
    RunnerAssignment.objects.create(agent=a, runner=r1, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=r2, rank=1)
    assert _rows(a, Turn.ORIGIN_EMAIL) == [r1, r2]


def test_a_non_strict_rule_puts_its_runner_first_then_the_defaults():
    a, laptop, cloud = _agent(), _runner("jj-mbp"), _runner("cloud-1")
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB)
    assert _rows(a, Turn.ORIGIN_ACE_WEB) == [cloud, laptop]
    assert _rows(a, Turn.ORIGIN_EMAIL) == [laptop]  # other sources untouched


def test_a_priority_runner_already_in_the_defaults_is_not_duplicated():
    a, laptop, cloud = _agent(), _runner("jj-mbp"), _runner("cloud-1")
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=1)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB)
    assert _rows(a, Turn.ORIGIN_ACE_WEB) == [cloud, laptop]


def test_the_composed_list_is_renumbered_from_zero():
    """The cascade compares ranks to decide who blocks whom, so a composed list
    that kept its source rows' rank=0 alongside a default rank=0 would make two
    runners each other's better rank."""
    a, laptop, cloud = _agent(), _runner("jj-mbp"), _runner("cloud-1")
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0, source=Turn.ORIGIN_ACE_WEB)
    defaults, priorities = services.load_assignment_rows([a.id])
    composed = services.assignment_rows_for(a.id, Turn.ORIGIN_ACE_WEB, defaults, priorities)
    assert [rank for rank, _r in composed] == [0, 1]


def test_a_strict_rule_returns_only_its_runner():
    a, laptop, cloud = _agent(), _runner("jj-mbp"), _runner("cloud-1")
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL, strict=True
    )
    assert _rows(a, Turn.ORIGIN_EMAIL) == [laptop]


def test_a_disabled_rule_falls_back_to_the_default_list():
    a, laptop, cloud = _agent(), _runner("jj-mbp"), _runner("cloud-1")
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL, strict=True, enabled=False
    )
    assert _rows(a, Turn.ORIGIN_EMAIL) == [cloud]


def test_a_disabled_default_row_is_excluded_as_before():
    a, r1, r2 = _agent(), _runner("r1"), _runner("r2")
    RunnerAssignment.objects.create(agent=a, runner=r1, rank=0, enabled=False)
    RunnerAssignment.objects.create(agent=a, runner=r2, rank=1)
    assert _rows(a, Turn.ORIGIN_API) == [r2]


def test_a_rule_routes_to_a_runner_switched_off_in_the_default_list():
    """The one place `enabled` means two things, pinned deliberately.

    A rule is its OWN row with its own toggle, so switching a runner off in the
    default order does not switch off the rule that names it. Defensible — the
    operator edited two separate things — but it means a chip greyed under
    "Default order" can still be answering email, so the UI says so and this
    test stops the behaviour changing by accident.
    """
    a, laptop = _agent(), _runner("jj-mbp")
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0, enabled=False)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL)
    assert _rows(a, Turn.ORIGIN_EMAIL) == [laptop]
    assert _rows(a, Turn.ORIGIN_API) == []  # …and the default list stays empty


def test_no_agents_loads_nothing():
    assert services.load_assignment_rows([]) == ({}, {})
