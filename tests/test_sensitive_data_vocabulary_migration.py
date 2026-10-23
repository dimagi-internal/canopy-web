"""The data migration to the sensitive-data vocabulary (harness 0108): every stored
`zdr` becomes `sensitive_data`, and every `data: phi` becomes `data: sensitive` — in
the parsed interface and in the YAML as saved, comments kept."""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

# needs_migrations: it walks the real migration graph (ci.yml, `Backend pytest (migrations)`).
pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("harness", "0107_runner_directed"), ("canopy_sessions", "0048_message_sealed_encrypted")]
AFTER = [("harness", "0108_sensitive_data_vocabulary")]

SOURCE = "capabilities:\n  data:\n    sites: [connect-labs]\n    data: phi   # vouched\n"


def test_every_stored_copy_takes_the_new_words():
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps
    User = old.get_model("auth", "User")
    Workspace = old.get_model("workspaces", "Workspace")
    Runner = old.get_model("harness", "Runner")
    RunnerFlag = old.get_model("harness", "RunnerFlag")
    Session = old.get_model("canopy_sessions", "Session")
    Agent = old.get_model("agents", "Agent")
    owner = User.objects.create(username="jj", email="jjackson@dimagi.com")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    box = Runner.objects.create(name="cloud-zdr-1", kind="cloud", owner=owner,
                                capabilities={"data_mode": "zdr", "sessions": True})
    RunnerFlag.objects.create(runner=box, flag="zdr", declared_by=owner)
    hot = Session.objects.create(workspace=ws, metadata={"runner_requirements": ["zdr"], "x": 1})
    cold = Session.objects.create(workspace=ws, metadata={"x": 1})
    agent = Agent.objects.create(
        slug="labsdata", name="labsdata", workspace=ws, owner=owner, interface_source=SOURCE,
        interface={"capabilities": {"data": {"sites": ["connect-labs"], "data": "phi"}}})

    executor.loader.build_graph()
    executor.migrate(AFTER)
    new = executor.loader.project_state(AFTER).apps
    box = new.get_model("harness", "Runner").objects.get(pk=box.pk)
    assert box.capabilities == {"data_mode": "sensitive_data", "sessions": True}
    assert list(new.get_model("harness", "RunnerFlag").objects.values_list("flag", flat=True)) == [
        "sensitive_data"]
    sessions = new.get_model("canopy_sessions", "Session").objects
    assert sessions.get(pk=hot.pk).metadata == {"runner_requirements": ["sensitive_data"], "x": 1}
    assert sessions.get(pk=cold.pk).metadata == {"x": 1}
    agent = new.get_model("agents", "Agent").objects.get(pk=agent.pk)
    assert agent.interface["capabilities"]["data"]["data"] == "sensitive"
    assert agent.interface_source == SOURCE.replace("data: phi", "data: sensitive")
