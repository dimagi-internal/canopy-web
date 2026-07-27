"""fork() on both adapters — copy/trim semantics, both modes, and edits.

The two adapters MUST behave identically through the read model: pre-fork steps
land 'complete' (with a seeded verdict), the fork step onward lands 'pending',
and only kept-step decisions carry forward per `mode` (+ any `edits`).
"""
from __future__ import annotations

import datetime as dt

import pytest

from apps.agent_runs.models import (
    AgentRun,
    AgentRunDecision,
    AgentRunStep,
)
from apps.agent_runs.schemas import Decision, Run, Step
from apps.agent_runs.stores import DbRunStore, InMemoryRunStore
from apps.agents.models import Agent
from apps.workspaces.testing import a_workspace


# --------------------------------------------------------------------------
# InMemory fixtures
# --------------------------------------------------------------------------
def _inmem_run() -> Run:
    return Run(
        id="r1",
        agent_slug="echo",
        label="demo",
        mode="review",
        current_step="build",
        created_at=dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc),
        steps=[
            Step(key="spec", ordinal=0, title="Spec", status="complete"),
            Step(key="build", ordinal=1, title="Build", status="running"),
            Step(key="ship", ordinal=2, title="Ship", status="pending"),
        ],
        decisions=[
            Decision(step_key="spec", question="persona?", ai_default="flw", status="ai-default"),
            Decision(step_key="spec", question="tone?", ai_default="formal", override="casual", status="overridden"),
            Decision(step_key="build", question="framework?", ai_default="react", status="ai-default"),
        ],
    )


def _inmem_store() -> InMemoryRunStore:
    store = InMemoryRunStore()
    store.put_run(_inmem_run())
    return store


# --------------------------------------------------------------------------
# InMemory: structure + both modes + edits
# --------------------------------------------------------------------------
def test_inmem_fork_trims_steps_and_seeds_verdicts():
    store = _inmem_store()
    summary = store.fork("echo", "r1", at_step="build", mode="keep-all")
    assert summary.forked_from == "r1"
    assert summary.current_step == "build"
    assert summary.status == "in_progress"  # spec complete, build/ship pending

    forked = store.get_run("echo", summary.id)
    by_key = {s.key: s.status for s in forked.steps}
    assert by_key == {"spec": "complete", "build": "pending", "ship": "pending"}
    # pre-fork step carries a seeded verdict; nothing for pending steps
    assert [v.step_key for v in forked.verdicts] == ["spec"]
    assert forked.verdicts[0].criteria == {"seeded": True}
    # the original run is untouched
    src = store.get_run("echo", "r1")
    assert {s.key: s.status for s in src.steps}["build"] == "running"


def test_inmem_fork_keep_overrides_only_drops_ai_defaults():
    store = _inmem_store()
    summary = store.fork("echo", "r1", at_step="build", mode="keep-overrides-only")
    forked = store.get_run("echo", summary.id)
    # only spec's overridden decision survives; spec's ai-default + build's drop
    assert [(d.step_key, d.question) for d in forked.decisions] == [("spec", "tone?")]
    assert forked.decisions[0].status == "overridden"


def test_inmem_fork_keep_all_keeps_kept_step_decisions():
    store = _inmem_store()
    summary = store.fork("echo", "r1", at_step="build", mode="keep-all")
    forked = store.get_run("echo", summary.id)
    # both spec decisions carry; build's decision is NOT a kept step → dropped
    questions = {(d.step_key, d.question) for d in forked.decisions}
    assert questions == {("spec", "persona?"), ("spec", "tone?")}


def test_inmem_fork_applies_edits():
    store = _inmem_store()
    edits = {"spec": {"persona?": {"override": "supervisor", "reasoning": "wider reach"}}}
    summary = store.fork("echo", "r1", at_step="build", mode="keep-all", edits=edits)
    forked = store.get_run("echo", summary.id)
    persona = next(d for d in forked.decisions if d.question == "persona?")
    assert persona.override == "supervisor"
    assert persona.status == "overridden"
    assert persona.reasoning == "wider reach"


def test_inmem_fork_rejects_unknown_mode_and_step():
    store = _inmem_store()
    with pytest.raises(ValueError):
        store.fork("echo", "r1", at_step="build", mode="bogus")
    with pytest.raises(ValueError):
        store.fork("echo", "r1", at_step="nope")


# --------------------------------------------------------------------------
# DB adapter
# --------------------------------------------------------------------------
pytestmark = pytest.mark.django_db


@pytest.fixture
def db_run():
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())
    run = AgentRun.objects.create(agent=agent, label="demo", mode=AgentRun.REVIEW, current_step="build")
    spec = AgentRunStep.objects.create(run=run, key="spec", ordinal=0, title="Spec", status=AgentRunStep.COMPLETE)
    build = AgentRunStep.objects.create(run=run, key="build", ordinal=1, title="Build", status=AgentRunStep.RUNNING)
    AgentRunStep.objects.create(run=run, key="ship", ordinal=2, title="Ship", status=AgentRunStep.PENDING)
    AgentRunDecision.objects.create(step=spec, question="persona?", ai_default="flw", status=AgentRunDecision.AI_DEFAULT)
    AgentRunDecision.objects.create(step=spec, question="tone?", ai_default="formal", override="casual", status=AgentRunDecision.OVERRIDDEN)
    # on the (non-kept) fork step → must NOT carry forward in any mode
    AgentRunDecision.objects.create(step=build, question="framework?", ai_default="react", status=AgentRunDecision.AI_DEFAULT)
    return run


def test_db_fork_creates_real_rows_and_trims(db_run):
    store = DbRunStore()
    summary = store.fork("echo", str(db_run.pk), at_step="build", mode="keep-all")
    assert summary.id != str(db_run.pk)
    assert summary.forked_from == str(db_run.pk)
    assert summary.current_step == "build"
    assert summary.status == "in_progress"

    forked = store.get_run("echo", summary.id)
    assert {s.key: s.status for s in forked.steps} == {
        "spec": "complete", "build": "pending", "ship": "pending"
    }
    # seeded verdict on the kept step
    assert [v.step_key for v in forked.verdicts] == ["spec"]
    assert forked.verdicts[0].criteria == {"seeded": True}
    # forked_from FK set on the ORM row
    new_run = AgentRun.objects.get(pk=summary.id)
    assert new_run.forked_from_id == db_run.pk


def test_db_fork_keep_overrides_only(db_run):
    store = DbRunStore()
    summary = store.fork("echo", str(db_run.pk), at_step="build", mode="keep-overrides-only")
    forked = store.get_run("echo", summary.id)
    assert [(d.step_key, d.question) for d in forked.decisions] == [("spec", "tone?")]


def test_db_fork_keep_all_and_edits(db_run):
    store = DbRunStore()
    edits = {"spec": {"persona?": {"override": "supervisor"}}}
    summary = store.fork("echo", str(db_run.pk), at_step="build", mode="keep-all", edits=edits)
    forked = store.get_run("echo", summary.id)
    questions = {(d.step_key, d.question) for d in forked.decisions}
    assert questions == {("spec", "persona?"), ("spec", "tone?")}
    persona = next(d for d in forked.decisions if d.question == "persona?")
    assert persona.override == "supervisor"
    assert persona.status == "overridden"


def test_db_fork_rejects_unknown_mode_and_step(db_run):
    store = DbRunStore()
    with pytest.raises(ValueError):
        store.fork("echo", str(db_run.pk), at_step="build", mode="bogus")
    with pytest.raises(ValueError):
        store.fork("echo", str(db_run.pk), at_step="nope")
