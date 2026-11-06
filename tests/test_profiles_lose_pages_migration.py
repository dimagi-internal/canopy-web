"""`pages` leaves every stored profile, and `interface_source` leaves the code and the table (agents 0132-0134).
Delete once all three have deployed."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("agents", "0131_read_mode_enters_through_answer_caller")]
STRIPPED = [("agents", "0132_profiles_lose_pages")]
FORGOTTEN = [("agents", "0133_forget_interface_source")]


def _state(targets):
    MigrationExecutor(connection).migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


@pytest.fixture(autouse=True)
def back_to_the_top():
    yield
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def _agents(default_workspace, docs: dict):
    apps = _state(BEFORE)
    Agent = apps.get_model("agents", "Agent")
    user = get_user_model().objects.create_user("o", "o@dimagi.com", "pw")
    for slug, caps in docs.items():
        Agent.objects.create(slug=slug, name=slug, workspace_id=default_workspace.pk, owner_id=user.pk,
                             interface={"capabilities": caps}, interface_source="kept: yaml\n")


def test_the_empty_pages_key_goes_and_nothing_else_moves(default_workspace):
    site = {"tools": ["Read"], "pages": [], "sites": ["connect-labs"], "ceiling": ["mcp__*x__*"],
            "own_login": {"connect-labs": ["X_TOKEN"]}}
    _agents(default_workspace, {"ace": {"ask": {"tools": ["Read"], "pages": []}, "connect": site},
                                "plain": {}})
    Agent = _state(STRIPPED).get_model("agents", "Agent")
    caps = Agent.objects.get(slug="ace").interface["capabilities"]
    assert caps == {"ask": {"tools": ["Read"]},
                    "connect": {k: v for k, v in site.items() if k != "pages"}}
    assert Agent.objects.get(slug="plain").interface == {"capabilities": {}}


@pytest.mark.parametrize("caps, named", [
    ({"stock": {"tools": ["Read"], "pages": ["stock://*"]}}, "ace.stock selects by page"),
    ({"one": {"sites": ["connect-labs"]}, "two": {"sites": ["connect-labs"]}}, "connect-labs is named by one, two"),
])
def test_a_document_whose_answers_would_change_stops_the_migration(default_workspace, caps, named):
    _agents(default_workspace, {"ace": caps})
    with pytest.raises(RuntimeError, match=named):
        _state(STRIPPED)
    Agent = _state(BEFORE).get_model("agents", "Agent")
    assert Agent.objects.get(slug="ace").interface == {"capabilities": caps}   # nothing changed
    with connection.cursor() as cursor:   # or going back to the top meets it again; raw, as the
        cursor.execute("DELETE FROM agents_agent")   # historical model's cascade names tables not there yet


def test_a_new_agent_saves_without_naming_the_column(default_workspace):
    _agents(default_workspace, {})
    apps = _state(FORGOTTEN)
    Agent = apps.get_model("agents", "Agent")
    user = get_user_model().objects.get(username="o")
    Agent.objects.create(slug="new", name="new", workspace_id=default_workspace.pk, owner_id=user.pk)
    assert {f.name for f in Agent._meta.get_fields()} >= {"interface", "interface_published_at"}
    assert "interface_source" not in {f.name for f in Agent._meta.get_fields()}


def test_the_column_is_gone_at_the_top():
    """agents 0134 (PostgreSQL) or 0133 (anything else) drops it."""
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    with connection.cursor() as cursor:
        columns = [c.name for c in connection.introspection.get_table_description(cursor, "agents_agent")]
    assert "interface_source" not in columns and "interface" in columns
