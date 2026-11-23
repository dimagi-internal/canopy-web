"""connect-helper and connect-coach move from the `connect-messaging` site to `connect-labs`'s
`workers` surface (tokens 0048). Delete once that migration has deployed."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("tokens", "0047_reply_push"), ("agents", "0143_creator_agent_admins")]
AFTER = [("tokens", "0048_connect_workers_on_connect_labs")]

ENTRY = {"description": "Connect Workers over PersonalID", "entry": "/connect-helper:worker-chat",
         "tools": ["Read"], "bash": [], "read_paths": ["{cwd}/**"], "write_paths": [],
         "sites": ["connect-messaging"], "ceiling": ["worker_profile"], "contact_scoped": True}


def _state(targets):
    MigrationExecutor(connection).migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


@pytest.fixture(autouse=True)
def back_to_the_top():
    yield
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def _world(apps, *, mcp="https://labs.connect.dimagi.com/mcp/"):
    user = get_user_model().objects.create_user("o", "o@dimagi.com", "pw")
    ws = apps.get_model("workspaces", "Workspace").objects.create(slug="connect", display_name="Connect",
                                                                    created_by_id=user.pk)
    Agent, App = apps.get_model("agents", "Agent"), apps.get_model("tokens", "AppCredential")
    agents = {slug: Agent.objects.create(slug=slug, name=slug, workspace_id=ws.pk, owner_id=user.pk,
                                         interface={"capabilities": {"connect_messaging": dict(ENTRY)}})
              for slug in ("connect-helper", "connect-coach")}
    other = Agent.objects.create(slug="ace", name="ACE", workspace_id=ws.pk, owner_id=user.pk,
                                 interface={"capabilities": {"connect": {"sites": ["connect-labs"]}}})
    labs = App.objects.create(name="connect-labs", workspace_id=ws.pk, created_by_id=user.pk,
                              host_mcp_resource=mcp)
    old = App.objects.create(name="connect-messaging", workspace_id=ws.pk, created_by_id=user.pk)
    return agents, other, labs, old


def test_the_agents_serve_the_workers_surface_of_connect_labs_under_the_same_key():
    agents, other, labs, old = _world(_state(BEFORE))
    apps = _state(AFTER)
    Agent, App = apps.get_model("agents", "Agent"), apps.get_model("tokens", "AppCredential")
    for slug in agents:
        cap = Agent.objects.get(slug=slug).interface["capabilities"]["connect_messaging"]
        assert cap["sites"] == ["connect-labs"] and cap["surface"] == "workers"
        assert cap["contact_scoped"] is True and cap["ceiling"] == ["worker_profile"]
    assert Agent.objects.get(slug="ace").interface == other.interface   # anyone else is untouched
    allowed = set(apps.get_model("tokens", "AppCredentialAgent").objects.filter(app_id=labs.pk)
                  .values_list("agent__slug", flat=True))
    assert allowed == {"connect-helper", "connect-coach"}
    labs = App.objects.get(pk=labs.pk)
    assert (labs.reply_webhook_url, labs.reply_mode) == (
        "https://labs.connect.dimagi.com/messaging/canopy/replies/", "final")
    assert App.objects.get(pk=old.pk).revoked_at is not None


def test_another_labs_keeps_its_webhook_unset():
    _agents, _other, labs, _old = _world(_state(BEFORE), mcp="https://labs-staging.example.org/mcp/")
    apps = _state(AFTER)
    assert apps.get_model("tokens", "AppCredential").objects.get(pk=labs.pk).reply_webhook_url is None


def test_a_database_without_connect_labs_in_connect_changes_nothing():
    apps = _state(BEFORE)
    _agents, _other, labs, old = _world(apps)
    apps.get_model("tokens", "AppCredential").objects.filter(pk=labs.pk).delete()
    apps = _state(AFTER)
    Agent = apps.get_model("agents", "Agent")
    assert Agent.objects.get(slug="connect-helper").interface["capabilities"]["connect_messaging"]["sites"] == [
        "connect-messaging"]
    assert apps.get_model("tokens", "AppCredential").objects.get(pk=old.pk).revoked_at is None
