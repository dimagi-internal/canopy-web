"""0107 drops the 1Password columns and strips `op` pointers from `runtime_sources`.

A data migration's own test (walk back to BEFORE, seed, migrate to AFTER) lives only
until that migration has deployed — delete this file then (see CLAUDE.md).

needs_migrations: it walks the real migration graph, so CI runs it with the migrations
(`Backend pytest (PostgreSQL)`), not under `--nomigrations`.
"""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.workspaces.testing import a_workspace

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("agents", "0106_credential_alert_context")]
AFTER = [("agents", "0107_remove_1password")]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


def test_op_pointers_are_stripped_and_the_rest_of_each_entry_stays():
    old = _migrate(BEFORE)
    try:
        Agent = old.get_model("agents", "Agent")
        # Only `agents` is walked back, so the workspace row is made with the real model.
        ws = a_workspace("m0107")
        Agent.objects.create(
            slug="m0107-op", name="Op", workspace_id=ws.pk, credential_source="1password",
            op_vault="Agent-Op", runtime_secrets=["a", "b", "c", "d"],
            runtime_sources={
                "a": {"op": "op://Agent-Op/a/credential"},
                "b": {"value": "https://x", "op": "op://Agent-Op/b/credential"},
                "c": {"local_only": True},
                "d": "not-a-dict",
            })
        Agent.objects.create(slug="m0107-none", name="None", workspace_id=ws.pk, runtime_sources={})

        new = _migrate(AFTER)
        Agent = new.get_model("agents", "Agent")
        assert Agent.objects.get(slug="m0107-op").runtime_sources == {
            "a": {}, "b": {"value": "https://x"}, "c": {"local_only": True}, "d": "not-a-dict"}
        assert Agent.objects.get(slug="m0107-none").runtime_sources == {}
        columns = {f.name for f in Agent._meta.get_fields()}
        assert not {"credential_source", "op_vault", "op_sa_token_enc"} & columns
    finally:
        _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
