"""Migrate before a rollout, holding back a tail of pure drops until after it.

    manage.py migrate_for_rollout --noinput    # the deploy, before the rollout
    manage.py migrate --noinput                # the deploy, after the rollout

The migrate step runs before the service rolls, so the old task keeps serving for
about 3.5 minutes against the new schema. New columns don't bother it. A dropped
column does: Django SELECTs every column of a model, so on a table read on most
requests 30-40% of ALL requests fail for the whole window (canopy-web#2447; 10-30
had four such deploys, 119-332 errors each). A failed rollout is worse: it rolls
back to old code against a column that is gone.

Authors still write migrations the obvious way, with no expand/contract PRs
(CLAUDE.md). The pipeline does that split instead:

- A migration holding a RemoveField/DeleteModel is held back, along with anything
  that depends on it. New code no longer reads those columns, so it runs fine
  while they still exist, and old code keeps them until it is gone.
- The columns about to go are made nullable first. New code no longer writes them,
  and without this its INSERTs would fail NOT NULL until the drop runs.
- If anything held back is not itself a pure drop (an AddField or RunPython behind
  a drop in the same app), new code may need it before cutover, so nothing is held
  back and this is a plain `migrate`: today's behaviour, window included. A rename
  has no safe order either way and always lands here.

Any error while PLANNING also falls back to a plain `migrate`. An error while
APPLYING is a real migration failure and fails the deploy, as it always has.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS, connections, migrations
from django.db.migrations.executor import MigrationExecutor

# Whose presence makes a migration worth holding back.
DROPS = (migrations.RemoveField, migrations.DeleteModel)
# What may sit in a held-back migration. Everything else might be something new
# code needs before cutover, so it forces the fallback. The non-drops here change
# nothing new code relies on.
SAFE_AFTER_ROLLOUT = DROPS + (
    migrations.RemoveIndex,
    migrations.RemoveConstraint,
    migrations.AlterModelOptions,
    migrations.AlterModelManagers,
)


@dataclass
class Split:
    now: list = field(default_factory=list)
    later: list = field(default_factory=list)
    # Why nothing was held back, when something would have been.
    fallback_reason: str = ""


def split_plan(plan, graph) -> Split:
    """Split a forwards plan (Migration objects, in order) around its drops."""
    held = set()
    now, later = [], []
    for migration in plan:
        key = (migration.app_label, migration.name)
        # The graph's parents, not migration.dependencies: they have swappable
        # and run_before resolved.
        parents = {parent.key for parent in graph.node_map[key].parents}
        has_drop = any(isinstance(op, DROPS) for op in migration.operations)
        if has_drop or parents & held:
            held.add(key)
            later.append(migration)
        else:
            now.append(migration)

    for migration in later:
        for op in migration.operations:
            if not isinstance(op, SAFE_AFTER_ROLLOUT):
                reason = (
                    f"{migration.app_label}.{migration.name} is held back behind a drop and has a {type(op).__name__}"
                )
                return Split(now=list(plan), fallback_reason=reason)
    return Split(now=now, later=later)


def columns_to_relax(split: Split, loader) -> list[tuple[str, str]]:
    """(table, column) for each NOT NULL column the held-back migrations drop."""
    columns = []
    for migration in split.later:
        removes = [op for op in migration.operations if isinstance(op, migrations.RemoveField)]
        if not removes:
            continue
        state = loader.project_state((migration.app_label, migration.name), at_end=False)
        for op in removes:
            model = state.apps.get_model(migration.app_label, op.model_name)
            field_ = model._meta.get_field(op.name)
            # A many-to-many has no column here, and only NOT NULL needs relaxing.
            if field_.many_to_many or field_.null or not field_.column:
                continue
            columns.append((model._meta.db_table, field_.column))
    return columns


class Command(BaseCommand):
    help = "Migrate for a rollout: a tail of pure drops waits for the post-rollout migrate."

    def add_arguments(self, parser):
        # Accepted so the workflow's two commands read alike; this never prompts.
        parser.add_argument("--noinput", "--no-input", action="store_true")

    def handle(self, *args, **options):
        connection = connections[DEFAULT_DB_ALIAS]
        try:
            executor = MigrationExecutor(connection)
            graph = executor.loader.graph
            plan = [m for m, _ in executor.migration_plan(graph.leaf_nodes())]
            split = split_plan(plan, graph)
            relax = columns_to_relax(split, executor.loader)
        except Exception as exc:  # noqa: BLE001 - planning must never cost a deploy
            return self._plain(f"could not plan ({exc!r})")

        if split.fallback_reason:
            return self._plain(split.fallback_reason)
        if not split.later:
            return self._plain("nothing to hold back")

        with connection.schema_editor() as editor:
            for table, column in relax:
                self.stdout.write(f"Dropping NOT NULL on {table}.{column}")
                editor.execute(
                    f"ALTER TABLE {editor.quote_name(table)} ALTER COLUMN {editor.quote_name(column)} DROP NOT NULL"
                )

        # An explicit plan, never targets: `migrate <app> <name>` on an applied
        # node migrates BACKWARDS, and one app's target can get applied as another
        # target's dependency.
        if split.now:
            executor.migrate(
                [(m.app_label, m.name) for m in split.now],
                plan=[(m, False) for m in split.now],
            )
        held = ", ".join(f"{m.app_label}.{m.name}" for m in split.later)
        self.stdout.write(f"Applied {len(split.now)} migration(s). Held back until after the rollout: {held}")

    def _plain(self, why: str) -> None:
        self.stdout.write(f"Plain migrate: {why}")
        call_command("migrate", interactive=False, stdout=self.stdout)
