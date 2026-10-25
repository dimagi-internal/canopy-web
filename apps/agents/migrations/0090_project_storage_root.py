from django.db import migrations, models


def freeze_roots(apps, schema_editor):
    """Every project made before #2047 keeps the root its objects are stored under: the one
    its served file names (a project renamed since stored under its old address), else its
    address now."""
    projects = apps.get_model("agents", "AgentProject")
    kbs = apps.get_model("agents", "ProjectService")
    served = dict(kbs.objects.filter(kind="kb").exclude(kb_key="").values_list("project_id", "kb_key"))
    for pk, workspace, handle in projects.objects.values_list("pk", "workspace_id", "handle"):
        key = served.get(pk, "")
        root = key[:key.rindex("/kb/") + 1] if "/kb/" in key else f"workspace-projects/{workspace}/{handle}/"
        projects.objects.filter(pk=pk).update(storage_root=root)


class Migration(migrations.Migration):

    dependencies = [
        ('agents', '0089_remove_kb_drive_source'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentproject',
            name='storage_root',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.RunPython(freeze_roots, migrations.RunPython.noop),
    ]
