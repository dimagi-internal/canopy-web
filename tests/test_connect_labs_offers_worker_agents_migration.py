"""`connect-labs` may offer connect-helper and connect-coach, and `connect-messaging` is revoked
(tokens 0048). Delete once that migration has deployed."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("tokens", "0047_reply_push"), ("agents", "0143_creator_agent_admins")]
AFTER = [("tokens", "0048_connect_labs_offers_worker_agents")]

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


def test_connect_labs_may_offer_both_agents_and_connect_messaging_is_revoked():
    agents, other, labs, old = _world(_state(BEFORE))
    apps = _state(AFTER)
    Agent, App = apps.get_model("agents", "Agent"), apps.get_model("tokens", "AppCredential")
    allowed = set(apps.get_model("tokens", "AppCredentialAgent").objects.filter(app_id=labs.pk)
                  .values_list("agent__slug", flat=True))
    assert allowed == {"connect-helper", "connect-coach"}
    for slug in agents:   # each agent's own entry is its admin's to change, not this migration's
        assert Agent.objects.get(slug=slug).interface["capabilities"]["connect_messaging"] == ENTRY
    assert Agent.objects.get(slug="ace").interface == other.interface
    labs = App.objects.get(pk=labs.pk)
    assert labs.reply_webhook_url is None   # a workspace owner's change, behind a passkey
    assert App.objects.get(pk=old.pk).revoked_at is not None


def test_running_it_twice_changes_nothing():
    _agents, _other, labs, _old = _world(_state(BEFORE))
    apps = _state(AFTER)
    from importlib import import_module

    import_module("apps.tokens.migrations.0048_connect_labs_offers_worker_agents").forward(apps, None)
    assert apps.get_model("tokens", "AppCredentialAgent").objects.filter(app_id=labs.pk).count() == 2


def test_a_database_without_connect_labs_in_connect_changes_nothing():
    apps = _state(BEFORE)
    _agents, _other, labs, old = _world(apps)
    apps.get_model("tokens", "AppCredential").objects.filter(pk=labs.pk).delete()
    apps = _state(AFTER)
    assert not apps.get_model("tokens", "AppCredentialAgent").objects.exists()
    assert apps.get_model("tokens", "AppCredential").objects.get(pk=old.pk).revoked_at is None
