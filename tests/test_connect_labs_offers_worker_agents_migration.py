"""`connect-labs` may offer connect-helper and connect-coach, and `connect-messaging` is revoked
(tokens 0048). Its SQL runs against today's tables, which carry every column it touches, so the
test needs no walk back through the migration graph. Delete once that migration has deployed."""
from __future__ import annotations

from importlib import import_module

import pytest
from django.contrib.auth import get_user_model
from django.db import connection

from apps.agents.models import Agent
from apps.tokens.models import AppCredential, AppCredentialAgent
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

SQL = import_module("apps.tokens.migrations.0048_connect_labs_offers_worker_agents").SQL


def forward():
    with connection.cursor() as cursor:
        for statement in SQL:
            cursor.execute(statement)

ENTRY = {"description": "Connect Workers over PersonalID", "entry": "/connect-helper:worker-chat",
         "tools": ["Read"], "bash": [], "read_paths": ["{cwd}/**"], "write_paths": [],
         "sites": ["connect-messaging"], "ceiling": ["worker_profile"], "contact_scoped": True}


@pytest.fixture()
def world():
    user = get_user_model().objects.create_user("o", "o@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=user)
    agents = {slug: Agent.objects.create(slug=slug, name=slug, workspace=ws, owner=user,
                                         interface={"capabilities": {"connect_messaging": dict(ENTRY)}})
              for slug in ("connect-helper", "connect-coach")}
    other = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=user,
                                 interface={"capabilities": {"connect": {"sites": ["connect-labs"]}}})
    labs = AppCredential.objects.create(name="connect-labs", workspace=ws, created_by=user,
                                        host_mcp_resource="https://labs.connect.dimagi.com/mcp/")
    old = AppCredential.objects.create(name="connect-messaging", workspace=ws, created_by=user)
    return {"agents": agents, "other": other, "labs": labs, "old": old}


def _allowed(app) -> set[str]:
    return set(AppCredentialAgent.objects.filter(app=app).values_list("agent__slug", flat=True))


def test_connect_labs_may_offer_both_agents_and_connect_messaging_is_revoked(world):
    forward()
    assert _allowed(world["labs"]) == {"connect-helper", "connect-coach"}
    for slug in world["agents"]:   # each agent's own entry is its admin's to change, not this migration's
        assert Agent.objects.get(slug=slug).interface["capabilities"]["connect_messaging"] == ENTRY
    assert Agent.objects.get(slug="ace").interface == world["other"].interface
    labs = AppCredential.objects.get(pk=world["labs"].pk)
    assert labs.reply_webhook_url is None   # a workspace owner's change, behind a passkey
    assert AppCredential.objects.get(pk=world["old"].pk).revoked_at is not None


def test_running_it_twice_changes_nothing(world):
    forward()
    forward()
    assert AppCredentialAgent.objects.filter(app=world["labs"]).count() == 2


def test_a_database_without_connect_labs_in_connect_changes_nothing(world):
    world["labs"].delete()
    forward()
    assert not AppCredentialAgent.objects.exists()
    assert AppCredential.objects.get(pk=world["old"].pk).revoked_at is None
