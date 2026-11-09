"""A `use:ask` (or `use:read`) access line becomes a `read_only` line.

Read mode is a role now: `read_only` opens it on every agent.
A line that
opened the one capability `ask` meant exactly that, so it is rewritten to the role it was standing in
for. Nothing else moves: each agent's published `ask` stays its read-mode override under its own name,
and stored turn stamps and admission rules are never rewritten (`interface.is_read_mode` is the alias).

If the same subject already holds a role line, the capability line added nothing and is dropped. Every
affected agent's (or workspace's) old lines are kept in the settings audit (`SettingChange`, operation
`read_mode_migration`), as `0118` did.

Its own migration, ahead of any schema change to grants: it updates rows.
"""
from django.db import migrations

_READ_MODE = ("ask", "read")
_SNAPSHOT = ("id", "subject_key", "role", "capability", "manual_only")


def _snapshot(line):
    return {f: getattr(line, f) for f in _SNAPSHOT}


def forwards(apps, schema_editor):
    Grant = apps.get_model("agents", "AgentGrant")
    Change = apps.get_model("events", "SettingChange")
    scopes: dict[tuple, list] = {}
    for line in Grant.objects.filter(role="", capability__in=_READ_MODE).order_by("id"):
        scopes.setdefault((line.agent_id, line.workspace_id), []).append(line)
    for (agent_id, workspace_id), lines in scopes.items():
        before = [_snapshot(ln) for ln in lines]
        for line in lines:
            base = line.subject_key.partition("#")[0]
            held = Grant.objects.filter(agent_id=agent_id, workspace_id=workspace_id, subject_key=base)
            if held.exists():
                line.delete()          # the subject already holds a role (>= read_only)
                continue
            line.role, line.capability, line.subject_key, line.manual_only = "read_only", "", base, False
            line.save(update_fields=["role", "capability", "subject_key", "manual_only"])
        after = [_snapshot(x) for x in Grant.objects.filter(agent_id=agent_id, workspace_id=workspace_id)
                 .order_by("id")]
        if agent_id is not None:
            agent = apps.get_model("agents", "Agent").objects.get(pk=agent_id)
            ws, target, scope = agent.workspace_id, agent.slug, f"access-list:{agent.slug}"
        else:
            ws, target, scope = workspace_id, workspace_id, f"agent-defaults:{workspace_id}"
        Change.objects.create(
            workspace_id=ws, actor=None, actor_email="", via="system",
            operation="read_mode_migration", method="MIGRATE", path="agents/0119", target=target,
            summary=f"{target}: a use:ask line became read_only; old lines kept here"[:500],
            changes=[{"scope": scope, "changed": True, "before": before, "after": after}])


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0118_roles_and_capability_lines"),
        ("events", "0009_session_usage_workspace_required"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
