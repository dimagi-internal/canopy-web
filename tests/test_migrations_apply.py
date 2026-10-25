"""Every migration applies to an empty database: the test DB here is built by
replaying the whole graph, so reaching the assertion is most of the proof.

needs_migrations: it is the one standing test of `Backend pytest (migrations)`
(ci.yml). A data migration's own test (walk back to BEFORE, seed, migrate to
AFTER) lives only until that migration has deployed — see CLAUDE.md."""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = [pytest.mark.django_db, pytest.mark.needs_migrations]


def test_the_whole_graph_is_applied():
    executor = MigrationExecutor(connection)
    assert executor.migration_plan(executor.loader.graph.leaf_nodes()) == []
