"""Link every access-list line that names a role to that role's row.

`AgentGrant.role_def` is the row of the line's role in the catalog of its own tree: an agent's
line takes the agent's workspace root, a workspace default its workspace's root
(`docs/superpowers/specs/2026-11-04-configurable-roles-design.md`, section 6). The seeded rows carry
the permissions the code table always gave, so what a line grants does not move. A role string that
is not one of the built-ins (`0118` and `0119` removed every other) aborts the migration naming the
line rather than guessing. Every tree's links are kept in the settings audit (`SettingChange`,
operation `configurable_roles_migration`), as `0118` and `0119` did.

Its own migration, ahead of any schema change to grants: it updates rows.
"""
from django.db import migrations

_MAX_DEPTH = 32


def link_lines(apps):
    """Set `role_def` on every role line to its tree's row; returns {root: [(line id, key)]}.
    Idempotent: a line already linked is rewritten to the same value."""
    Grant = apps.get_model("agents", "AgentGrant")
    Role = apps.get_model("agents", "AgentRole")
    Workspace = apps.get_model("workspaces", "Workspace")
    Agent = apps.get_model("agents", "Agent")
    parents = dict(Workspace.objects.values_list("pk", "parent_id"))
    homes = dict(Agent.objects.values_list("pk", "workspace_id"))

    def root(slug):
        for _ in range(_MAX_DEPTH):
            if not parents.get(slug):
                return slug
            slug = parents[slug]
        return slug

    rows = {(r.workspace_id, r.key): r.pk for r in Role.objects.filter(builtin=True)}
    plan: dict[tuple, list[int]] = {}
    for line in Grant.objects.exclude(role="").only("id", "role", "agent_id", "workspace_id"):
        home = homes.get(line.agent_id) if line.agent_id else line.workspace_id
        tree = root(home)
        if (tree, line.role) not in rows:
            raise RuntimeError(f"access-list line {line.pk} names role {line.role!r}, which is not a built-in "
                               f"role of {tree!r}; fix or remove it before this migration can run")
        plan.setdefault((tree, line.role), []).append(line.pk)
    out: dict[str, list] = {}
    for (tree, key), ids in plan.items():
        Grant.objects.filter(pk__in=ids).update(role_def_id=rows[(tree, key)])
        out.setdefault(tree, []).extend((i, key) for i in ids)
    return out


def forwards(apps, schema_editor):
    Change = apps.get_model("events", "SettingChange")
    for tree, linked in sorted(link_lines(apps).items()):
        Change.objects.create(
            workspace_id=tree, actor=None, actor_email="", via="system",
            operation="configurable_roles_migration", method="MIGRATE", path="agents/0123", target=tree,
            summary=f"{tree}: {len(linked)} access-list lines now point at their role's row"[:500],
            changes=[{"scope": f"agent-roles:{tree}", "changed": True, "before": None,
                      "after": {"linked": [{"line": i, "role": k} for i, k in linked]}}])


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0122_seed_builtin_roles"),
        ("events", "0009_session_usage_workspace_required"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
