"""0118 maps the old ladder onto permission bundles and named-capability lines.

old editor -> editor; old viewer -> read_only (+ a capability line per other viewer-level
capability, so nobody loses a door); old user -> a capability line per capability that role could
open; a capability loses `role:`; every affected agent's old lines and interface go to the audit.

A data migration's own test (walk back to BEFORE, seed, migrate to AFTER) lives only until that
migration has deployed — delete this file then (see CLAUDE.md).

needs_migrations: it walks the real migration graph, so CI runs it with the migrations.
"""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.workspaces.testing import a_workspace

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("agents", "0116_delete_agentdelegation")]
AFTER = [("agents", "0118_roles_and_capability_lines")]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


def _line(Grant, agent=None, workspace=None, *, kind="workspace", role, key, ws=None, domain="",
          verified=False, manual_only=False):
    return Grant.objects.create(
        agent_id=getattr(agent, "pk", None), workspace_id=workspace, kind=kind, role=role,
        subject_key=key, subject_workspace_id=ws, domain=domain, verified=verified, manual_only=manual_only)


def _got(Grant, **where):
    return sorted((g.kind, g.role, g.capability, g.subject_key, g.manual_only)
                  for g in Grant.objects.filter(**where))


def test_the_old_ladder_becomes_permission_bundles_and_capability_lines():
    old = _migrate(BEFORE)
    try:
        Agent, Grant = old.get_model("agents", "Agent"), old.get_model("agents", "AgentGrant")
        ws = a_workspace("m0118")
        caps = {"ask": {"role": "viewer", "tools": ["Read"]},
                "connect": {"role": "viewer", "sites": ["connect-labs"]},
                "canopy": {"role": "user", "tools": ["Grep"]}}
        ace = Agent.objects.create(slug="m0118-ace", name="Ace", workspace_id=ws.pk,
                                   interface={"version": 1, "capabilities": caps},
                                   interface_source="capabilities:\n  ask:\n    role: viewer  # kept? no\n")
        _line(Grant, ace, kind="contact", role="viewer", key="contact:", verified=True)
        _line(Grant, ace, role="user", key=f"workspace:{ws.pk}::", ws=ws.pk, manual_only=True)
        _line(Grant, ace, role="editor", key=f"workspace:{ws.pk}:editor:", ws=ws.pk, manual_only=True)
        # `ask` was a user-level capability here: a viewer line must not newly open it.
        strict = Agent.objects.create(slug="m0118-strict", name="Strict", workspace_id=ws.pk,
                                      interface={"version": 1, "capabilities": {"ask": {"role": "user"}}})
        _line(Grant, strict, kind="domain", role="viewer", key="domain:dimagi.com", domain="dimagi.com",
              verified=True)
        # nothing published: a user line admitted nobody to anything; a viewer line is kept as read_only.
        bare = Agent.objects.create(slug="m0118-bare", name="Bare", workspace_id=ws.pk)
        _line(Grant, bare, role="user", key=f"workspace:{ws.pk}::", ws=ws.pk)
        _line(Grant, bare, kind="domain", role="viewer", key="domain:dimagi.com", domain="dimagi.com")
        # a workspace default: over the capabilities of the agents in it and below.
        _line(Grant, workspace=ws.pk, kind="domain", role="user", key="domain:dimagi.com",
              domain="dimagi.com", verified=True)

        new = _migrate(AFTER)
        Agent, Grant = new.get_model("agents", "Agent"), new.get_model("agents", "AgentGrant")
        Change = new.get_model("events", "SettingChange")
        ws_pk = ws.pk

        ace = Agent.objects.get(slug="m0118-ace")
        assert _got(Grant, agent=ace) == sorted([
            ("contact", "read_only", "", "contact:", False),                       # viewer -> read_only
            ("contact", "", "connect", "contact:#use:connect", False),             # ...and keeps its other door
            ("workspace", "", "ask", f"workspace:{ws_pk}::#use:ask", False),       # user -> a line per capability
            ("workspace", "", "canopy", f"workspace:{ws_pk}::#use:canopy", False),
            ("workspace", "", "connect", f"workspace:{ws_pk}::#use:connect", False),
            ("workspace", "editor", "", f"workspace:{ws_pk}:editor:", True),       # editor untouched, gate kept
        ])
        assert not any("role" in c for c in ace.interface["capabilities"].values())
        assert "role" not in ace.interface_source
        kept = Change.objects.get(operation="access_roles_migration", target="m0118-ace")
        scopes = {c["scope"]: c for c in kept.changes}
        assert scopes["interface:m0118-ace"]["before"]["interface"]["capabilities"]["ask"]["role"] == "viewer"
        assert {x["role"] for x in scopes["access-list:m0118-ace"]["before"]} == {"viewer", "user"}

        assert _got(Grant, agent=Agent.objects.get(slug="m0118-strict")) == []
        assert _got(Grant, agent=Agent.objects.get(slug="m0118-bare")) == [
            ("domain", "read_only", "", "domain:dimagi.com", False)]

        # the default: names from every agent under the workspace (ask, connect, canopy from ace).
        assert [(g[2]) for g in _got(Grant, workspace_id=ws_pk)] == ["ask", "canopy", "connect"]
        assert Change.objects.filter(operation="access_roles_migration", target=ws_pk).exists()
    finally:
        _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
