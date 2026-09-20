"""agents.0027's uuid backfill, against rows that already have a value.

The whole suite was green while prod's `Run database migrations` step failed
three times in a row. The gap: every test database reaches 0027 with no
AgentTask rows, and this bug needs two. `AddField` with a callable default
evaluates the callable ONCE and stamps that single value onto every existing
row — so nothing is left null, a backfill guarded on `uuid__isnull=True`
matched nothing, and the `AlterField` to unique died on the duplicates.

Reproduced first on a sqlite copy with three tasks:
    IntegrityError: UNIQUE constraint failed: new__agents_agenttask.uuid

The invariant is therefore "every row is rewritten, whatever it holds now", and
that is what this asserts. It is checked against the migration's OWN function so
the test moves if the migration does, and a real unique index cannot be used to
stage the duplicates (it would reject them on the way in) — so the rows are a
stand-in, and the question asked of them is which ones the backfill touches.
"""
from __future__ import annotations

import uuid as uuidlib

import importlib

# A module name starting with a digit cannot be imported with `import x.y`.
fill_uuids = importlib.import_module(
    "apps.agents.migrations.0027_task_absorbs_item").fill_uuids


class Rows:
    """The three pre-existing tasks, all stamped with one value by AddField."""

    def __init__(self, count: int, value):
        self.values = {pk: value for pk in range(1, count + 1)}

    # -- the slice of the ORM the backfill uses -----------------------------
    @property
    def objects(self):
        return self

    def values_list(self, field, flat=False):
        assert (field, flat) == ("pk", True)
        return self

    def iterator(self):
        return iter(list(self.values))

    def filter(self, **kw):
        if "uuid__isnull" in kw:
            # The bug, made visible: nothing is null, so a guarded backfill
            # would find nothing to do.
            keep = [pk for pk, v in self.values.items() if (v is None) == kw["uuid__isnull"]]
            return _Subset(self, keep)
        return _Subset(self, [kw["pk"]])


class _Subset:
    def __init__(self, rows: Rows, pks):
        self.rows, self.pks = rows, pks

    def values_list(self, field, flat=False):
        return self

    def iterator(self):
        return iter(self.pks)

    def update(self, **kw):
        for pk in self.pks:
            self.rows.values[pk] = kw["uuid"]
        return len(self.pks)


class FakeApps:
    def __init__(self, rows):
        self.rows = rows

    def get_model(self, app, model):
        assert (app, model) == ("agents", "AgentTask")
        return self.rows


def test_every_existing_task_gets_its_own_uuid():
    shared = uuidlib.uuid4()          # what AddField gave all of them
    rows = Rows(3, shared)
    fill_uuids(FakeApps(rows), None)
    assert len(set(rows.values.values())) == 3, (
        "each pre-existing task must end up with its own uuid — the unique index "
        "added next in this migration is what fails otherwise"
    )
    assert shared not in rows.values.values()
