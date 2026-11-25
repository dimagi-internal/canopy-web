"""Fail a PR whose new migrations would break the release still serving during a deploy.

    manage.py check_migration_rollout --base <sha>    # CI, on every PR

The deploy migrates before the service rolls, so the previous release serves for about
3.5 minutes against the new schema. `migrate_for_rollout` protects it from pure drops by
holding them until after the rollout. It cannot protect it from:

- a rename of a column or table (RenameField, RenameModel, AlterModelTable, an AlterField
  that changes db_column): it is not a drop, so it runs before the rollout, and every
  query the old release makes on that model fails;
- raw SQL that drops or renames a column or table;
- a drop that has an AddField or RunPython behind it in the same app: the whole deploy
  falls back to a plain `migrate`, and the dropped column vanishes under the old release.

Each of these is a PR-time failure here, with the fix. A migration whose window is
genuinely fine (a table nothing reads on a request path) says so on a line of its own:

    # rollout-safe: <why the old release does not read it>
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import migrations
from django.db.migrations.loader import MigrationLoader

from apps.common.management.commands.migrate_for_rollout import split_plan

MARKER = re.compile(r"^\s*#\s*rollout-safe:\s*\S", re.MULTILINE)
RAW_SQL_BREAK = re.compile(r"\b(DROP\s+(COLUMN|TABLE)|RENAME\s+(COLUMN|TO)\b)", re.IGNORECASE)
_NAME = r'"?(\w+)"?'
# What a raw statement takes away: (table, column), or (table, None) for the whole table.
RAW_SQL_TARGETS = (
    re.compile(rf"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_NAME}\s+DROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?{_NAME}", re.I),
    re.compile(rf"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_NAME}\s+RENAME\s+COLUMN\s+{_NAME}", re.I),
    re.compile(rf"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_NAME}\s+RENAME\s+TO\b()", re.I),
    re.compile(rf"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_NAME}()", re.I),
)
MIGRATION_FILE = re.compile(r"^(?:.*/)?migrations/\d{4}_[^/]*\.py$")

RENAME_FIX = (
    "Add the new column, copy the data and point the code at it in this PR; drop the old "
    "one in a later PR (the pipeline holds a pure drop until after the rollout)."
)
SQL_FIX = (
    "Do the drop as a RemoveField/DeleteModel the pipeline can hold back, or do the "
    "rename as add-copy-then-drop across two PRs."
)
FALLBACK_FIX = (
    "Put the drop in a migration of its own, last in its app, with anything new in an "
    "earlier migration; or ship the drop in a later PR."
)


@dataclass(frozen=True)
class Problem:
    migration: str
    what: str
    fix: str

    def __str__(self) -> str:
        return f"{self.migration}: {self.what}\n    fix: {self.fix}"


def _database_ops(operations):
    """Every operation that reaches the database, unwrapping SeparateDatabaseAndState."""
    for op in operations:
        if isinstance(op, migrations.SeparateDatabaseAndState):
            yield from _database_ops(op.database_operations)
        else:
            yield op


def _sql_text(sql) -> str:
    if isinstance(sql, (list, tuple)):
        return " ".join(_sql_text(s) for s in sql)
    return str(sql)


def _live_columns(state) -> set[tuple[str, str | None]]:
    """(table, column) and (table, None) for everything the models in `state` read."""
    live = set()
    for model in state.apps.get_models():
        table = model._meta.db_table
        live.add((table, None))
        live.update((table, f.column) for f in model._meta.local_concrete_fields)
    return live


def _raw_sql_breaks(sql: str, before_state) -> str | None:
    """The statement in `sql` that takes away something the previous release reads, if any.

    A column the models stopped naming in an earlier deploy is the safe second step of a
    drop, so it passes. Without a state, or for a statement it cannot read, it flags.
    """
    hit = RAW_SQL_BREAK.search(sql)
    if not hit:
        return None
    if before_state is None:
        return hit.group(0)
    targets = [(m.group(1), m.group(2) or None) for pattern in RAW_SQL_TARGETS for m in pattern.finditer(sql)]
    if not targets:
        return hit.group(0)
    live = _live_columns(before_state)
    for table, column in targets:
        if (table, column) in live:
            return f"{hit.group(0)} {table}.{column}" if column else f"{hit.group(0)} {table}"
    return None


def column_breaks(migration, before_state=None) -> list[str]:
    """What, in one migration applied before the rollout, the previous release cannot survive."""
    found = []
    for op in _database_ops(migration.operations):
        name = type(op).__name__
        if isinstance(op, migrations.RenameField):
            found.append(f"{name} {op.model_name}.{op.old_name} -> {op.new_name}")
        elif isinstance(op, migrations.RenameModel):
            found.append(f"{name} {op.old_name} -> {op.new_name}")
        elif isinstance(op, migrations.AlterModelTable):
            found.append(f"{name} {op.name} -> {op.table}")
        elif isinstance(op, migrations.AlterField) and before_state is not None:
            old = before_state.models.get((migration.app_label, op.model_name_lower))
            old_field = old.fields.get(op.name) if old else None
            if old_field is not None and old_field.db_column != op.field.db_column:
                found.append(f"{name} {op.model_name}.{op.name} changes db_column")
        elif isinstance(op, migrations.RunSQL):
            broken = _raw_sql_breaks(_sql_text(op.sql), before_state)
            if broken:
                found.append(f"RunSQL that drops or renames ({broken})")
    return found


def problems(plan, graph, before_state=lambda migration: None) -> list[Problem]:
    """Problems with a forwards plan of new migrations, as the deploy would run it."""
    split = split_plan(plan, graph)
    if split.fallback_reason:
        culprit = split.fallback_reason.split(" ", 1)[0]
        return [Problem(culprit, f"falls back to a plain migrate: {split.fallback_reason}", FALLBACK_FIX)]
    found = []
    for migration in split.now:
        label = f"{migration.app_label}.{migration.name}"
        for what in column_breaks(migration, before_state(migration)):
            fix = SQL_FIX if what.startswith("RunSQL") else RENAME_FIX
            found.append(Problem(label, f"runs before the rollout: {what}", fix))
    return found


def _added_migration_files(base: str) -> list[Path]:
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=A", f"{base}...HEAD"],
        cwd=settings.BASE_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [Path(settings.BASE_DIR) / p for p in out.split() if MIGRATION_FILE.match(p)]


class Command(BaseCommand):
    help = "Fail when new migrations would break the release still serving during a deploy."

    def add_arguments(self, parser):
        parser.add_argument("--base", required=True, help="the commit the PR branches from")

    def handle(self, *args, base, **options):
        try:
            added = {p.resolve() for p in _added_migration_files(base)}
        except subprocess.CalledProcessError as exc:
            raise CommandError(f"git diff against {base} failed: {exc.stderr}") from exc
        if not added:
            self.stdout.write("No new migrations.")
            return

        loader = MigrationLoader(None, ignore_no_migrations=True)
        graph = loader.graph
        new, waived = {}, []
        for key, migration in graph.nodes.items():
            path = Path(sys.modules[type(migration).__module__].__file__).resolve()
            if path not in added:
                continue
            if MARKER.search(path.read_text()):
                waived.append(f"{key[0]}.{key[1]}")
            else:
                new[key] = migration

        plan, seen = [], set()
        for leaf in graph.leaf_nodes():
            for key in graph.forwards_plan(leaf):
                if key in new and key not in seen:
                    seen.add(key)
                    plan.append(new[key])

        found = problems(
            plan,
            graph,
            lambda m: loader.project_state((m.app_label, m.name), at_end=False),
        )
        for label in waived:
            self.stdout.write(f"{label}: marked rollout-safe, not checked")
        if not found:
            self.stdout.write(f"{len(plan)} new migration(s) are safe for the running release.")
            return
        for problem in found:
            self.stderr.write(f"::error::{problem}")
        raise CommandError(
            f"{len(found)} migration problem(s) would break the release still serving during the deploy. "
            "If the window is genuinely fine, add `# rollout-safe: <why>` to the migration."
        )
