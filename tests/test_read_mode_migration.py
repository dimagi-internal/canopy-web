"""A `use:ask` line becomes `read_only` (agents 0119). Delete once that migration has deployed."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("agents", "0118_roles_and_capability_lines")]
AFTER = [("agents", "0119_use_ask_lines_become_read_only")]


def _state(targets):
    MigrationExecutor(connection).migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


@pytest.fixture(autouse=True)
def back_to_the_top():
    yield
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def test_stamped_read_mode_lines_become_read_only_and_a_duplicate_is_dropped(default_workspace):
    apps = _state(BEFORE)
    Agent, Grant = apps.get_model("agents", "Agent"), apps.get_model("agents", "AgentGrant")
    user = get_user_model().objects.create_user("o", "o@dimagi.com", "pw")
    agent = Agent.objects.create(slug="ace", name="Ace", workspace_id=default_workspace.pk, owner_id=user.pk)
    ask = Grant.objects.create(agent=agent, kind="contact", domain="dimagi.com", capability="ask",
                               subject_key="contact:dimagi.com#use:ask")
    other = Grant.objects.create(agent=agent, kind="contact", domain="x.org", capability="canopy",
                                 subject_key="contact:x.org#use:canopy")
    dup_role = Grant.objects.create(agent=agent, kind="workspace", subject_workspace_id=default_workspace.pk,
                                    role="read_write", subject_key=f"workspace:{default_workspace.pk}::")
    dup_cap = Grant.objects.create(agent=agent, kind="workspace", subject_workspace_id=default_workspace.pk,
                                   capability="read",
                                   subject_key=f"workspace:{default_workspace.pk}::#use:read")
    default = Grant.objects.create(workspace_id=default_workspace.pk, kind="domain", domain="a.org",
                                   capability="ask", subject_key="domain:a.org#use:ask")

    apps = _state(AFTER)
    Grant = apps.get_model("agents", "AgentGrant")
    ask = Grant.objects.get(pk=ask.pk)
    assert (ask.role, ask.capability, ask.subject_key) == ("read_only", "", "contact:dimagi.com")
    assert Grant.objects.get(pk=other.pk).capability == "canopy"          # a real host site is untouched
    assert Grant.objects.get(pk=dup_role.pk).role == "read_write"
    assert not Grant.objects.filter(pk=dup_cap.pk).exists()               # already held a role
    default = Grant.objects.get(pk=default.pk)
    assert (default.role, default.capability, default.subject_key) == ("read_only", "", "domain:a.org")
    changes = apps.get_model("events", "SettingChange").objects.filter(operation="read_mode_migration")
    assert {c.target for c in changes} == {"ace", default_workspace.pk}
    assert changes.get(target="ace").changes[0]["before"][0]["capability"] in ("ask", "read")
