"""agents 0101 drops `AgentCredential.value_enc`, refusing when a value lives only
there. Delete this file once a deploy includes 0101 (CLAUDE.md § Migrations)."""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]


def _with_agents_at(name):
    """Every app at its newest migration, except agents at `name` — so the
    historical models of the other apps match their tables."""
    leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    return [n for n in leaves if n[0] != "agents"] + [("agents", name)]


BEFORE = "0100_vault_entry_account"
AFTER = "0101_drop_custodial_agent_values"


def _at(name):
    executor = MigrationExecutor(connection)
    executor.migrate([("agents", name)])
    executor.loader.build_graph()
    return executor.loader.project_state(_with_agents_at(name)).apps


def _agent(apps, slug):
    User = apps.get_model("auth", "User")
    Workspace = apps.get_model("workspaces", "Workspace")
    Agent = apps.get_model("agents", "Agent")
    user, _ = User.objects.get_or_create(username="mig")
    ws, _ = Workspace.objects.get_or_create(slug="mig", defaults={"display_name": "mig", "created_by": user})
    return Agent.objects.create(slug=slug, name=slug, workspace=ws)


def test_a_value_held_only_custodially_stops_the_migration():
    apps = _at(BEFORE)
    AgentCredential = apps.get_model("agents", "AgentCredential")
    AgentCredential.objects.create(agent=_agent(apps, "old"), name="api-key", value_enc="gAAAA-fernet")
    try:
        with pytest.raises(RuntimeError, match="old:api-key"):
            _at(AFTER)
    finally:
        AgentCredential.objects.all().delete()
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


def test_a_stale_custodial_copy_beside_ciphertext_is_dropped():
    apps = _at(BEFORE)
    AgentCredential = apps.get_model("agents", "AgentCredential")
    AgentCredential.objects.create(agent=_agent(apps, "zk"), name="api-key",
                                   value_enc="gAAAA-fernet", value_zk="zk1:abc", kv=1)
    apps = _at(AFTER)
    row = apps.get_model("agents", "AgentCredential").objects.get(name="api-key")
    assert row.value_zk == "zk1:abc"
    assert "value_enc" not in {f.name for f in row._meta.get_fields()}
    MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
