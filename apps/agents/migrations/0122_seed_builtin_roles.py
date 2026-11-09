"""Seed the three built-in agent roles on every root workspace.

`AgentRole` rows: `read_only`,
`read_write` and `editor` with the permissions they have always had, closed under the dependency
rule. A child workspace sees them through its chain. It only inserts into the new table; the lines
are linked to these rows by the next migration, which updates rows other tables reference and so
goes in its own.
"""
from django.db import migrations

# Frozen copies, never imported from roles.py: a migration must keep meaning what it meant.
_SEED = (
    ("read_only", "Read only", "Can ask the agent questions in read mode.", ["read"]),
    ("read_write", "Read and write", "Can drive the whole agent.", ["read", "use:*", "write"]),
    ("editor", "Editor", "Can drive the whole agent and change its skills and capabilities.",
     ["agent_edit", "read", "use:*", "write"]),
)


def forwards(apps, schema_editor):
    Workspace = apps.get_model("workspaces", "Workspace")
    Role = apps.get_model("agents", "AgentRole")
    for slug in Workspace.objects.filter(parent__isnull=True).values_list("pk", flat=True):
        for position, (key, label, description, permissions) in enumerate(_SEED):
            Role.objects.get_or_create(
                workspace_id=slug, key=key,
                defaults=dict(label=label, description=description, permissions=permissions,
                              builtin=True, position=position))


class Migration(migrations.Migration):
    dependencies = [("agents", "0121_agent_roles")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
