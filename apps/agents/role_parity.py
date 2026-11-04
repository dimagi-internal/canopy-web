"""Prove that every access-list line still grants what it granted before roles became rows.

`check()` reads every line that names a role and compares what it grants now (its role's ROW,
`AgentGrant.permissions`) with what the code table gave it before
(`roles.ROLES.permissions_of`, the built-ins' defaults). Run it against live data before the
migration (`manage.py check_agent_roles`: it says whether every role string is one the migration can
link) and after it (every line linked, to the row of its own tree, granting the same set).

A built-in row somebody has since EDITED differs on purpose and is reported as `edited`, not as a
mismatch. Read-only: it writes nothing.
"""
from __future__ import annotations

from django.db import connection

from apps.workspaces.models import Workspace, ancestor_chains

from . import roles
from .models import Agent, AgentGrant, AgentRole


def _table_exists() -> bool:
    return AgentRole._meta.db_table in connection.introspection.table_names()


def check() -> dict:
    """{lines, trees, unknown, unlinked, wrong_tree, mismatched, edited, ok}. Every list holds
    `{line, where, role, ...}` dicts naming the line; `ok` is true when nothing needs a person."""
    parents = dict(Workspace.objects.values_list("slug", "parent_id"))
    chains = ancestor_chains(parents)
    homes = dict(Agent.objects.values_list("pk", "workspace_id"))
    report = {"lines": 0, "trees": set(), "unknown": [], "unlinked": [], "wrong_tree": [],
              "mismatched": [], "edited": [], "migrated": _table_exists()}
    qs = AgentGrant.objects.exclude(role="").order_by("pk")
    if report["migrated"]:
        qs = qs.select_related("role_def")
    for line in qs:
        report["lines"] += 1
        home = homes.get(line.agent_id) if line.agent_id else line.workspace_id
        tree = (chains.get(home) or [home])[-1]
        report["trees"].add(tree)
        where = f"agent {line.agent_id}" if line.agent_id else f"defaults of {line.workspace_id}"
        who = {"line": line.pk, "where": where, "role": line.role, "tree": tree}
        if line.role not in roles.SEED_KEYS:
            report["unknown"].append(who)
            continue
        if not report["migrated"]:
            continue
        row = line.role_def
        if row is None:
            report["unlinked"].append(who)
            continue
        if row.workspace_id != tree or row.key != line.role:
            report["wrong_tree"].append({**who, "row": f"{row.key} on {row.workspace_id}"})
            continue
        before = roles.ROLES.permissions_of(line.role)
        after = roles.ROLES.close(row.permissions)
        if after != before:
            entry = {**who, "before": sorted(before), "after": sorted(after)}
            (report["edited"] if row.builtin else report["mismatched"]).append(entry)
    report["trees"] = sorted(report["trees"])
    report["ok"] = not (report["unknown"] or report["unlinked"] or report["wrong_tree"]
                        or report["mismatched"])
    return report
