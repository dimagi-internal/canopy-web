"""record_decision() write path round-trips through the read model on both stores."""
from __future__ import annotations

import datetime as dt

import pytest

from apps.agent_runs.models import AgentRun, AgentRunStep
from apps.agent_runs.schemas import Run, Step
from apps.agent_runs.stores import DbRunStore, InMemoryRunStore
from apps.agents.models import Agent
from apps.workspaces.testing import a_workspace


def test_inmem_record_decision_roundtrip():
    store = InMemoryRunStore()
    store.put_run(Run(
        id="r1", agent_slug="echo", label="demo",
        created_at=dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc),
        steps=[Step(key="spec", ordinal=0, status="running")],
    ))
    decision = store.record_decision("echo", "r1", "spec", {
        "question": "which persona?",
        "ai_default": "flw",
        "override": "supervisor",
        "status": "overridden",
        "reasoning": "wider reach",
        "evidence_basis": "interview notes",
    })
    assert decision.step_key == "spec"
    assert decision.status == "overridden"

    # read it back through the full read model
    run = store.get_run("echo", "r1")
    assert len(run.decisions) == 1
    got = run.decisions[0]
    assert got.step_key == "spec"
    assert got.question == "which persona?"
    assert got.ai_default == "flw"
    assert got.override == "supervisor"
    assert got.status == "overridden"
    assert got.reasoning == "wider reach"
    assert got.evidence_basis == "interview notes"


def test_inmem_record_decision_marks_run_changed():
    store = InMemoryRunStore()
    store.put_run(Run(
        id="r1", agent_slug="echo",
        steps=[Step(key="spec", ordinal=0, status="running")],
    ))
    _, cursor = store.changed_ids("echo")
    store.record_decision("echo", "r1", "spec", {"question": "q?"})
    ids, _ = store.changed_ids("echo", cursor)
    assert ids == ["r1"]


@pytest.mark.django_db
def test_db_record_decision_roundtrip():
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())
    run = AgentRun.objects.create(agent=agent, label="demo")
    AgentRunStep.objects.create(run=run, key="spec", ordinal=0, status=AgentRunStep.RUNNING)
    store = DbRunStore()
    rid = str(run.pk)

    decision = store.record_decision("echo", rid, "spec", {
        "question": "which persona?",
        "ai_default": "flw",
        "override": "supervisor",
        "status": "overridden",
        "reasoning": "wider reach",
        "evidence_basis": "interview notes",
    })
    assert decision.step_key == "spec"

    read = store.get_run("echo", rid)
    assert len(read.decisions) == 1
    got = read.decisions[0]
    assert got.question == "which persona?"
    assert got.ai_default == "flw"
    assert got.override == "supervisor"
    assert got.status == "overridden"
    assert got.reasoning == "wider reach"
    assert got.evidence_basis == "interview notes"


@pytest.mark.django_db
def test_db_record_decision_defaults_status():
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())
    run = AgentRun.objects.create(agent=agent, label="demo")
    AgentRunStep.objects.create(run=run, key="spec", ordinal=0, status=AgentRunStep.RUNNING)
    store = DbRunStore()
    rid = str(run.pk)
    store.record_decision("echo", rid, "spec", {"question": "q?", "ai_default": "x"})
    got = store.get_run("echo", rid).decisions[0]
    assert got.status == "ai-default"  # model default
    assert got.ai_default == "x"
