"""Roles are rows: a root workspace starts with the built-ins, and a line points at its row."""
from __future__ import annotations

import pytest
from django.core.management import call_command

from apps.agents import role_parity, rolecatalog, roles
from apps.agents.models import Agent, AgentGrant, AgentRole
from apps.workspaces.testing import a_user, a_workspace

pytestmark = pytest.mark.django_db


def test_a_new_root_workspace_is_seeded_with_the_builtins(default_workspace):
    rows = {r.key: r for r in AgentRole.objects.filter(workspace=default_workspace)}
    assert list(rows) == list(roles.SEED_KEYS) or set(rows) == set(roles.SEED_KEYS)
    for seed in roles.SEED:
        row = rows[seed.key]
        assert row.builtin and row.label == seed.label
        # stored CLOSED: write carries read and every host site, agent_edit carries write
        assert set(row.permissions) == roles.ROLES.close(seed.permissions)
    assert "write" in rows["editor"].permissions and "agent_edit" in rows["editor"].permissions


def test_a_child_workspace_has_no_rows_of_its_own_and_sees_its_roots(default_workspace):
    child = a_workspace("connect", parent=default_workspace)
    assert not AgentRole.objects.filter(workspace=child).exists()
    assert rolecatalog.root_of(child.pk) == default_workspace.pk
    assert rolecatalog.role_for(child.pk, "editor").workspace_id == default_workspace.pk
    assert rolecatalog.role_for(child.pk, "no_such_role") is None


def test_ensure_seeded_keeps_an_edit(default_workspace):
    row = AgentRole.objects.get(workspace=default_workspace, key="read_only")
    row.label = "Reader"
    row.save()
    rolecatalog.ensure_seeded(default_workspace.pk)
    assert AgentRole.objects.get(pk=row.pk).label == "Reader"


def test_a_line_saved_by_role_name_points_at_its_trees_row(default_workspace):
    child = a_workspace("connect", parent=default_workspace)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=child, owner=a_user())
    line = AgentGrant.objects.create(agent=agent, kind=AgentGrant.CONTACT, role=roles.READ_ONLY)
    default = AgentGrant.objects.create(workspace=child, kind=AgentGrant.WORKSPACE,
                                        subject_workspace=child, role=roles.EDITOR)
    cap = AgentGrant.objects.create(agent=agent, kind=AgentGrant.DOMAIN, domain="x.org", capability="connect")
    root = AgentRole.objects.filter(workspace=default_workspace)
    assert line.role_def == root.get(key="read_only")
    assert default.role_def == root.get(key="editor")
    assert cap.role_def is None
    # changing the role through update_fields moves the row with it
    line.role = roles.READ_WRITE
    line.save(update_fields=["role"])
    assert AgentGrant.objects.get(pk=line.pk).role_def == root.get(key="read_write")


def test_a_row_cannot_be_deleted_while_a_line_holds_it(default_workspace):
    from django.db.models import ProtectedError

    agent = Agent.objects.create(slug="ace", name="Ace", workspace=default_workspace, owner=a_user())
    line = AgentGrant.objects.create(agent=agent, kind=AgentGrant.CONTACT, role=roles.READ_ONLY)
    with pytest.raises(ProtectedError):
        line.role_def.delete()


def test_the_parity_check_passes_on_lines_it_just_linked(default_workspace, capsys):
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=default_workspace, owner=a_user())
    for i, role in enumerate(roles.SEED_KEYS):
        AgentGrant.objects.create(agent=agent, kind=AgentGrant.DOMAIN, domain=f"d{i}.org", role=role)
    report = role_parity.check()
    assert report["ok"] and report["lines"] == 3 and report["migrated"]
    call_command("check_agent_roles")
    assert "ok: every line grants what it granted" in capsys.readouterr().out


def test_the_database_refuses_a_role_line_without_its_row_and_a_row_without_a_role(default_workspace):
    from django.db import IntegrityError, transaction

    agent = Agent.objects.create(slug="ace", name="Ace", workspace=default_workspace, owner=a_user())
    line = AgentGrant.objects.create(agent=agent, kind=AgentGrant.DOMAIN, domain="a.org", role=roles.READ_ONLY)
    with pytest.raises(IntegrityError), transaction.atomic():
        AgentGrant.objects.filter(pk=line.pk).update(role_def=None)
    with pytest.raises(IntegrityError), transaction.atomic():
        AgentGrant.objects.filter(pk=line.pk).update(role="", capability="x")


def test_the_parity_check_reports_an_edited_builtin_as_intended(default_workspace):
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=default_workspace, owner=a_user())
    line = AgentGrant.objects.create(agent=agent, kind=AgentGrant.DOMAIN, domain="a.org", role=roles.READ_ONLY)
    AgentRole.objects.filter(key="read_only").update(permissions=["read", "use:connect"])
    report = role_parity.check()
    assert report["ok"] and [x["line"] for x in report["edited"]] == [line.pk]   # an edit is intended
