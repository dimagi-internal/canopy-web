"""PR-time check: new migrations must not break the release still serving during a deploy."""

from __future__ import annotations

from pathlib import Path

import yaml
from django.db import migrations, models
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.state import ModelState, ProjectState

from apps.common.management.commands.check_migration_rollout import MARKER, problems

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"

DROP = migrations.RemoveField("agent", "old")
ADD = migrations.AddField("agent", "new", models.IntegerField(default=0))
RENAME = migrations.RenameField("agent", "old", "new")


def build(*specs):
    """specs: (app, name, parents, operations). Returns (plan, graph)."""
    graph = MigrationGraph()
    plan = []
    for app, name, parents, operations in specs:
        migration = type("M", (migrations.Migration,), {"operations": operations})(name, app)
        graph.add_node((app, name), migration)
        for parent in parents:
            graph.add_dependency(migration, (app, name), parent)
        plan.append(migration)
    return plan, graph


def test_additions_and_a_pure_drop_pass():
    plan, graph = build(
        ("a", "1_add", [], [ADD]),
        ("a", "2_drop", [("a", "1_add")], [DROP]),
    )
    assert problems(plan, graph) == []


def test_a_rename_fails_because_it_runs_before_the_rollout():
    plan, graph = build(("a", "1_rename", [], [RENAME]))
    [problem] = problems(plan, graph)
    assert problem.migration == "a.1_rename"
    assert "RenameField agent.old -> new" in problem.what
    assert "later PR" in problem.fix


def test_model_and_table_renames_fail():
    plan, graph = build(
        ("a", "1", [], [migrations.RenameModel("Old", "New")]),
        ("a", "2", [("a", "1")], [migrations.AlterModelTable("new", "a_renamed")]),
    )
    assert [p.migration for p in problems(plan, graph)] == ["a.1", "a.2"]


def test_raw_sql_that_drops_a_column_fails_even_inside_separate_database_and_state():
    sql = migrations.RunSQL("ALTER TABLE a_agent DROP COLUMN old", migrations.RunSQL.noop)
    wrapped = migrations.SeparateDatabaseAndState(database_operations=[sql], state_operations=[DROP])
    plan, graph = build(("a", "1", [], [wrapped]))
    [problem] = problems(plan, graph)
    assert "DROP COLUMN" in problem.what


def _agent_state(*columns):
    state = ProjectState()
    fields = [("id", models.AutoField(primary_key=True))] + [(c, models.IntegerField()) for c in columns]
    state.add_model(ModelState("a", "agent", fields))
    return state


def test_raw_sql_dropping_a_column_the_models_still_read_fails():
    plan, graph = build(("a", "1", [], [migrations.RunSQL('ALTER TABLE "a_agent" DROP COLUMN "old"')]))
    [problem] = problems(plan, graph, lambda m: _agent_state("old"))
    assert "a_agent.old" in problem.what


def test_raw_sql_dropping_a_column_the_models_stopped_naming_passes():
    """The second step of a drop: an earlier deploy's code already stopped reading it."""
    plan, graph = build(("a", "1", [], [migrations.RunSQL('ALTER TABLE "a_agent" DROP COLUMN "old"')]))
    assert problems(plan, graph, lambda m: _agent_state()) == []


def test_raw_sql_dropping_a_live_table_fails():
    plan, graph = build(("a", "1", [], [migrations.RunSQL("DROP TABLE IF EXISTS a_agent")]))
    [problem] = problems(plan, graph, lambda m: _agent_state())
    assert "a_agent" in problem.what


def test_state_only_removal_passes():
    """Code stops naming a column; its database drop ships later. Nothing reaches the table now."""
    wrapped = migrations.SeparateDatabaseAndState(database_operations=[], state_operations=[DROP])
    plan, graph = build(("a", "1", [], [wrapped]))
    assert problems(plan, graph) == []


def test_a_drop_with_an_addition_behind_it_fails_as_a_fallback():
    plan, graph = build(
        ("a", "1_drop", [], [DROP]),
        ("a", "2_add", [("a", "1_drop")], [ADD]),
    )
    [problem] = problems(plan, graph)
    assert "plain migrate" in problem.what
    assert problem.migration == "a.2_add"


def test_an_alter_field_that_changes_db_column_fails():
    state = ProjectState()
    state.add_model(
        ModelState("a", "agent", [("id", models.AutoField(primary_key=True)), ("old", models.IntegerField())])
    )
    alter = migrations.AlterField("agent", "old", models.IntegerField(db_column="renamed"))
    plan, graph = build(("a", "1", [], [alter]))
    [problem] = problems(plan, graph, lambda m: state)
    assert "db_column" in problem.what


def test_the_waiver_marker_needs_a_reason():
    assert MARKER.search("# rollout-safe: nothing reads this table on a request")
    assert not MARKER.search("# rollout-safe:")
    assert not MARKER.search("x = 1  # not rollout-safe")


def test_pr_fast_checks_run_the_check_against_the_base():
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    runs = [step.get("run", "") for step in jobs["pr-fast-checks"]["steps"]]
    assert any("check_migration_rollout --base" in run for run in runs)
