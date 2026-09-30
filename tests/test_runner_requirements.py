"""Runner requirements: the vocabulary, a runner's declared flags, and reading a
session's requirements. Spec: docs/superpowers/specs/2026-09-30-zdr-runners-design.md"""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import runner_requirements as rr
from apps.harness.models import Runner, RunnerFlag, Turn
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


def _session(meta=None):
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=a_workspace())
    return Session.objects.create(agent=agent, workspace=agent.workspace, title="c",
                                  metadata=meta or {})


def test_a_runner_has_no_flags_until_one_is_declared():
    r = Runner.objects.create(name="box", kind=Runner.CLOUD, capabilities={})
    assert r.flags == frozenset()
    RunnerFlag.objects.create(runner=r, flag="zdr")
    assert Runner.objects.get(pk=r.pk).flags == frozenset({"zdr"})


def test_a_flag_is_declared_once_per_runner():
    r = Runner.objects.create(name="box", kind=Runner.CLOUD, capabilities={})
    RunnerFlag.objects.create(runner=r, flag="zdr")
    with pytest.raises(IntegrityError):
        RunnerFlag.objects.create(runner=r, flag="zdr")


def test_no_metadata_means_no_requirements():
    assert rr.requirements_of_session(_session()) == frozenset()


def test_a_session_requirement_is_read():
    assert rr.requirements_of_session(_session({"runner_requirements": ["zdr"]})) == {"zdr"}


@pytest.mark.parametrize("bad", ["zdr", [1], {"zdr": 1}, ["nope"]])
def test_a_malformed_value_is_unsatisfiable(bad):
    reqs = rr.requirements_of_session(_session({"runner_requirements": bad}))
    assert not rr.satisfies(frozenset({"zdr"}), reqs)


def test_a_turn_without_a_session_has_no_requirements():
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())
    t = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="k")
    assert rr.requirements_of(t) == frozenset()


def test_satisfies_is_a_subset_check():
    assert rr.satisfies(frozenset(), frozenset())
    assert rr.satisfies(frozenset({"zdr"}), frozenset())
    assert rr.satisfies(frozenset({"zdr"}), frozenset({"zdr"}))
    assert not rr.satisfies(frozenset(), frozenset({"zdr"}))


def test_describe_names_the_flags_for_people():
    assert rr.describe(frozenset({"zdr"})) == "ZDR"
