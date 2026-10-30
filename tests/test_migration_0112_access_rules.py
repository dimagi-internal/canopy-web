"""0112 moves every agent's access rules onto its access list and strips them from the interface.

A data migration's own test (walk back to BEFORE, seed, migrate to AFTER) lives only until that
migration has deployed — delete this file then (see CLAUDE.md).

needs_migrations: it walks the real migration graph, so CI runs it with the migrations
(`Backend pytest (PostgreSQL)`), not under `--nomigrations`.
"""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.workspaces.testing import a_workspace

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("agents", "0111_remove_agent_zk_enabled")]
AFTER = [("agents", "0113_remove_agent_uses_access_list")]

ACE = {  # the access-relevant parts of the interface actually published on 2026-10-30
    "version": 1,
    "full": ["contact@dimagi.com:verified", "member@dimagi.com"],
    "capabilities": {
        "ask": {"callers": ["contact", "member"], "tools": ["Read"], "bash": [], "read_paths": [],
                "write_paths": [], "input": {}, "pages": [], "sites": [], "ceiling": []},
        "canopy": {"callers": ["member"], "tools": ["Read"], "bash": [], "read_paths": [],
                   "write_paths": [], "input": {}, "pages": [], "sites": ["canopy-web"], "ceiling": []},
    },
    "callers_default": "none",
}
TRELLIS = {"version": 1, "editors": "confined", "full": [],
           "capabilities": {"ask": {"callers": ["member"], "tools": ["Read"]}}, "callers_default": "none"}


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


def _lines(Grant, agent):
    return {(g.kind, g.role, g.domain, g.verified, g.at_least, g.manual_only)
            for g in Grant.objects.filter(agent=agent)}


def test_rules_become_lines_the_interface_loses_them_and_the_old_one_is_kept_in_the_audit():
    old = _migrate(BEFORE)
    try:
        Agent = old.get_model("agents", "Agent")
        ws = a_workspace("m0112")
        Agent.objects.create(slug="m0112-ace", name="Ace", workspace_id=ws.pk, interface=ACE,
                             interface_source="full: [member@dimagi.com]  # a comment worth keeping")
        Agent.objects.create(slug="m0112-trellis", name="Trellis", workspace_id=ws.pk, interface=TRELLIS)
        Agent.objects.create(slug="m0112-default", name="Default", workspace_id=ws.pk,
                             interface={"version": 1, "full": [], "capabilities": {}, "callers_default": "none"})

        new = _migrate(AFTER)
        Agent, Grant = new.get_model("agents", "Agent"), new.get_model("agents", "AgentGrant")
        Change = new.get_model("events", "SettingChange")

        ace = Agent.objects.get(slug="m0112-ace")
        assert _lines(Grant, ace) == {
            ("contact", "editor", "dimagi.com", True, "", False),       # full: contact@dimagi.com:verified
            ("workspace", "editor", "dimagi.com", False, "", False),    # full: member@dimagi.com
            ("contact", "viewer", "", False, "", False),                # ask: contact
            ("workspace", "user", "", False, "", False),                # canopy: member (ask is viewer's)
            ("workspace", "editor", "", False, "editor", True),         # the implicit rule, gated
        }
        assert set(ace.interface) == {"version", "capabilities"}
        assert {n: c["role"] for n, c in ace.interface["capabilities"].items()} == {"ask": "viewer", "canopy": "user"}
        assert not any("callers" in c for c in ace.interface["capabilities"].values())
        assert "full" not in ace.interface and "callers_default" not in ace.interface
        assert "callers" not in ace.interface_source and "full" not in ace.interface_source
        kept = Change.objects.get(operation="access_list_migration", target="m0112-ace")
        assert kept.changes[0]["before"]["source"].endswith("a comment worth keeping")
        assert kept.changes[0]["before"]["interface"]["full"] == ACE["full"]

        # `editors: confined` is the absence of the gated editor line.
        assert _lines(Grant, Agent.objects.get(slug="m0112-trellis")) == {("workspace", "user", "", False, "", False)}
        # A default agent only had the implicit rule.
        assert _lines(Grant, Agent.objects.get(slug="m0112-default")) == {("workspace", "editor", "", False, "editor", True)}
        assert "uses_access_list" not in {f.name for f in Agent._meta.get_fields()}
    finally:
        _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
