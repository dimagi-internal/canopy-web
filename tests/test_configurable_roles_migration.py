"""Seeding roles and linking lines to them (agents 0122, 0123). Delete once 0123 has deployed."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("agents", "0121_agent_roles")]
AFTER = [("agents", "0123_link_lines_to_roles")]


def _state(targets):
    MigrationExecutor(connection).migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


@pytest.fixture(autouse=True)
def back_to_the_top():
    yield
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def test_roots_are_seeded_and_every_role_line_is_linked_to_its_trees_row(default_workspace):
    apps = _state(BEFORE)
    Agent, Grant = apps.get_model("agents", "Agent"), apps.get_model("agents", "AgentGrant")
    Workspace, Role = apps.get_model("workspaces", "Workspace"), apps.get_model("agents", "AgentRole")
    user = get_user_model().objects.create_user("o", "o@dimagi.com", "pw")
    Role.objects.all().delete()
    child = Workspace.objects.create(slug="connect", display_name="Connect", created_by_id=user.pk,
                                     parent_id=default_workspace.pk)
    other = Workspace.objects.create(slug="other", display_name="Other", created_by_id=user.pk)
    ace = Agent.objects.create(slug="ace", name="Ace", workspace_id=child.pk, owner_id=user.pk)
    oth = Agent.objects.create(slug="oth", name="Oth", workspace_id=other.pk, owner_id=user.pk)
    agent_line = Grant.objects.create(agent=ace, kind="contact", role="editor", subject_key="contact:")
    other_line = Grant.objects.create(agent=oth, kind="contact", role="read_only", subject_key="contact:")
    default_line = Grant.objects.create(workspace_id=child.pk, kind="domain", domain="a.org", role="read_write",
                                        subject_key="domain:a.org")
    cap_line = Grant.objects.create(agent=ace, kind="domain", domain="b.org", capability="connect",
                                    subject_key="domain:b.org#use:connect")

    apps = _state(AFTER)
    Grant, Role = apps.get_model("agents", "AgentGrant"), apps.get_model("agents", "AgentRole")
    assert set(Role.objects.values_list("workspace_id", flat=True)) == {default_workspace.pk, "other"}
    assert Role.objects.filter(workspace_id="other").count() == 3
    editor = Role.objects.get(workspace_id=default_workspace.pk, key="editor")
    assert sorted(editor.permissions) == ["agent_edit", "read", "use:*", "write"] and editor.builtin
    assert Grant.objects.get(pk=agent_line.pk).role_def_id == editor.pk       # the child's line: the root's row
    assert Grant.objects.get(pk=default_line.pk).role_def.key == "read_write"
    assert Grant.objects.get(pk=default_line.pk).role_def.workspace_id == default_workspace.pk
    assert Grant.objects.get(pk=other_line.pk).role_def.workspace_id == "other"   # another tree: its own row
    assert Grant.objects.get(pk=cap_line.pk).role_def_id is None
    changes = apps.get_model("events", "SettingChange").objects.filter(operation="configurable_roles_migration")
    assert {c.target for c in changes} == {default_workspace.pk, "other"}


def test_a_role_that_is_not_a_builtin_aborts_naming_the_line(default_workspace):
    apps = _state(BEFORE)
    Agent, Grant = apps.get_model("agents", "Agent"), apps.get_model("agents", "AgentGrant")
    user = get_user_model().objects.create_user("o", "o@dimagi.com", "pw")
    ace = Agent.objects.create(slug="ace", name="Ace", workspace_id=default_workspace.pk, owner_id=user.pk)
    line = Grant.objects.create(agent=ace, kind="contact", role="viewer", subject_key="contact:")
    with pytest.raises(RuntimeError, match=f"line {line.pk}"):
        MigrationExecutor(connection).migrate(AFTER)
    Grant.objects.filter(pk=line.pk).delete()      # so the teardown can migrate to the top


def test_a_straggler_written_after_the_link_is_linked_before_the_constraint(default_workspace):
    apps = _state(AFTER)
    Agent, Grant = apps.get_model("agents", "Agent"), apps.get_model("agents", "AgentGrant")
    user = get_user_model().objects.create_user("o", "o@dimagi.com", "pw")
    ace = Agent.objects.create(slug="ace", name="Ace", workspace_id=default_workspace.pk, owner_id=user.pk)
    # what the previous release writes while the deploy is under way: a role, no row
    line = Grant.objects.create(agent=ace, kind="contact", role="read_only", subject_key="contact:")
    apps = _state([("agents", "0125_role_line_matches_row")])
    got = apps.get_model("agents", "AgentGrant").objects.get(pk=line.pk)
    assert got.role_def.key == "read_only" and got.role_def.workspace_id == default_workspace.pk
