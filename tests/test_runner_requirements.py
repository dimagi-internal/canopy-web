"""Runner requirements: the vocabulary, a runner's declared flags, and reading a
session's requirements. Spec: docs/superpowers/specs/2026-09-30-sensitive-runners-design.md"""
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
    RunnerFlag.objects.create(runner=r, flag="sensitive_data")
    assert Runner.objects.get(pk=r.pk).flags == frozenset({"sensitive_data"})


def test_a_flag_is_declared_once_per_runner():
    r = Runner.objects.create(name="box", kind=Runner.CLOUD, capabilities={})
    RunnerFlag.objects.create(runner=r, flag="sensitive_data")
    with pytest.raises(IntegrityError):
        RunnerFlag.objects.create(runner=r, flag="sensitive_data")


def test_no_metadata_means_no_requirements():
    assert rr.requirements_of_session(_session()) == frozenset()


def test_a_session_requirement_is_read():
    assert rr.requirements_of_session(_session({"runner_requirements": ["sensitive_data"]})) == {"sensitive_data"}


@pytest.mark.parametrize("bad", ["sensitive_data", [1], {"sensitive_data": 1}, ["nope"]])
def test_a_malformed_value_is_unsatisfiable(bad):
    reqs = rr.requirements_of_session(_session({"runner_requirements": bad}))
    assert not rr.satisfies(frozenset({"sensitive_data"}), reqs)


def test_a_turn_without_a_session_has_no_requirements():
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())
    t = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="k")
    assert rr.requirements_of(t) == frozenset()


def test_satisfies_is_a_subset_check():
    assert rr.satisfies(frozenset(), frozenset())
    assert rr.satisfies(frozenset({"sensitive_data"}), frozenset())
    assert rr.satisfies(frozenset({"sensitive_data"}), frozenset({"sensitive_data"}))
    assert not rr.satisfies(frozenset(), frozenset({"sensitive_data"}))


def test_describe_names_the_flags_for_people():
    assert rr.describe(frozenset({"sensitive_data"})) == "sensitive-data"


# ---- a declaration counts only while the box reports the mode (spec 2026-10-22 §2) ---


def test_a_declared_sensitive_is_not_in_force_until_the_box_reports_sensitive_mode():
    r = Runner.objects.create(name="box", kind=Runner.CLOUD, capabilities={})
    RunnerFlag.objects.create(runner=r, flag="sensitive_data")
    r = Runner.objects.get(pk=r.pk)
    assert r.flags == frozenset({"sensitive_data"})  # declared: what the owner said
    assert rr.in_force(r) == frozenset()  # but the box does not say it runs in sensitive-data mode
    assert not rr.runner_satisfies(r, frozenset({"sensitive_data"}))
    for mode in ("", "zrd", "ZDR"):
        r.capabilities = {"data_mode": mode}
        assert not rr.runner_satisfies(r, frozenset({"sensitive_data"})), mode
    r.capabilities = {"data_mode": "sensitive_data"}
    assert rr.runner_satisfies(r, frozenset({"sensitive_data"}))


def test_a_box_reporting_sensitive_mode_without_a_declaration_is_not_sensitive():
    # The report is the box speaking for itself; only a person's declaration makes it sensitive-data.
    r = Runner.objects.create(name="box", kind=Runner.CLOUD, capabilities={"data_mode": "sensitive_data"})
    assert not rr.runner_satisfies(r, frozenset({"sensitive_data"}))


def test_the_heartbeat_records_the_mode_and_a_beat_without_it_clears_it():
    from apps.harness import services

    r = Runner.objects.create(name="box", kind=Runner.CLOUD, capabilities={"sessions": True})
    RunnerFlag.objects.create(runner=r, flag="sensitive_data")
    services.heartbeat(r, active_turn_ids=[], data_mode="sensitive_data", data_mode_hash="abc123")
    r = Runner.objects.get(pk=r.pk)
    assert r.capabilities["data_mode"] == "sensitive_data" and r.capabilities["data_mode_hash"] == "abc123"
    assert r.capabilities["sessions"] is True and rr.runner_satisfies(r, frozenset({"sensitive_data"}))
    services.heartbeat(r, active_turn_ids=[])  # older code, or sensitive-data mode turned off
    r = Runner.objects.get(pk=r.pk)
    assert r.capabilities["data_mode"] == "" and not rr.runner_satisfies(r, frozenset({"sensitive_data"}))

