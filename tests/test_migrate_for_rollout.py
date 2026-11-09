"""The deploy holds a tail of pure drops back until after the rollout."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from django.db import migrations, models
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.loader import MigrationLoader

from apps.common.management.commands.migrate_for_rollout import split_plan

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/deploy-labs.yml"

DROP = migrations.RemoveField("agent", "old")
ADD = migrations.AddField("agent", "new", models.IntegerField(default=0))


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


def names(items):
    return [m.name for m in items]


def test_a_lone_drop_waits_for_the_rollout():
    plan, graph = build(("a", "1_drop", [], [DROP]))
    split = split_plan(plan, graph)
    assert names(split.later) == ["1_drop"]
    assert split.now == [] and split.fallback_reason == ""


def test_additions_ahead_of_a_drop_still_run_before_the_rollout():
    plan, graph = build(
        ("a", "1_add", [], [ADD]),
        ("a", "2_drop", [("a", "1_add")], [DROP]),
    )
    split = split_plan(plan, graph)
    assert names(split.now) == ["1_add"]
    assert names(split.later) == ["2_drop"]


def test_another_apps_addition_is_not_held_back_by_a_drop():
    plan, graph = build(("a", "1_drop", [], [DROP]), ("b", "1_add", [], [ADD]))
    split = split_plan(plan, graph)
    assert names(split.now) == ["1_add"]
    assert names(split.later) == ["1_drop"]


@pytest.mark.parametrize(
    "op",
    [ADD, migrations.RunPython(migrations.RunPython.noop), migrations.RenameField("agent", "x", "y")],
    ids=["addfield", "runpython", "renamefield"],
)
def test_anything_new_behind_a_drop_means_plain_migrate(op):
    """New code may need it before cutover, so nothing can be held back."""
    plan, graph = build(
        ("a", "1_drop", [], [DROP]),
        ("a", "2_more", [("a", "1_drop")], [op]),
    )
    split = split_plan(plan, graph)
    assert names(split.now) == ["1_drop", "2_more"]
    assert split.later == []
    assert "a.2_more" in split.fallback_reason


def test_a_dependency_in_another_app_is_held_back_too():
    plan, graph = build(
        ("a", "1_drop", [], [DROP]),
        ("b", "1_drop_later", [("a", "1_drop")], [migrations.RemoveIndex("agent", "i")]),
    )
    split = split_plan(plan, graph)
    assert names(split.later) == ["1_drop", "1_drop_later"]


def test_no_drops_holds_nothing_back():
    plan, graph = build(("a", "1_add", [], [ADD]))
    split = split_plan(plan, graph)
    assert names(split.now) == ["1_add"]
    assert split.later == [] and split.fallback_reason == ""


@pytest.mark.parametrize(
    "prefixes,held,falls_back",
    [
        (["0108_"], ["0108_"], False),
        (["0109_", "0110_", "0111_"], ["0111_"], False),
        (["0112_", "0113_"], ["0113_"], False),
        (["0107_"], [], True),
    ],
    ids=["12b48187", "d232fe66", "da4c49de", "b7c81581"],
)
def test_replay_of_the_deploys_that_caused_the_issue(prefixes, held, falls_back):
    loader = MigrationLoader(None)
    by_prefix = {name[:5]: (app, name) for app, name in loader.disk_migrations if app == "agents"}
    if not all(prefix in by_prefix for prefix in prefixes):
        pytest.skip("those migrations were squashed")
    plan = [loader.graph.nodes[by_prefix[prefix]] for prefix in prefixes]
    split = split_plan(plan, loader.graph)
    assert [m.name[:5] for m in split.later] == held
    assert bool(split.fallback_reason) is falls_back


def _deploy_steps():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = next(j for j in workflow["jobs"].values() if j.get("name") == "Deploy to Fargate")
    return job["steps"]


def _index(steps, prefix):
    return next(i for i, s in enumerate(steps) if s.get("name", "").startswith(prefix))


def test_the_drops_run_after_the_rollout_and_only_if_it_succeeded():
    steps = _deploy_steps()
    rollout = _index(steps, "Deploy (CloudFormation")
    post = _index(steps, "Apply the drops")
    assert _index(steps, "Run database migrations") < rollout < post
    assert '"migrate","--noinput"' in steps[post]["run"]
    # No always()/failure(): a failed rollout rolls back to code that needs the column.
    assert "always()" not in steps[post]["if"] and "failure()" not in steps[post]["if"]
    assert "steps.pending.outputs.drops == 'true'" in steps[post]["if"]


def test_a_deploy_without_drops_runs_a_plain_migrate():
    run = _deploy_steps()[_index(_deploy_steps(), "Run database migrations")]["run"]
    assert "steps.pending.outputs.drops == 'true' && 'migrate_for_rollout' || 'migrate'" in run
