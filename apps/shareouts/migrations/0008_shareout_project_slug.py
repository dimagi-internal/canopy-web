"""Replace `Shareout.project` (an FK to the retired workbench Project) with a
plain `project_slug` string — the shape `Walkthrough.project_slug` already uses.

Three steps in one migration, in order: add the column, copy each row's
project slug into it (NULL stays NULL: the roll-up), drop the FK. projects/0013
(which drops the projects tables) depends on this, so the copy always reads a
live projects table.

Reverse re-adds the nullable FK empty: the slugs cannot be mapped back once the
project rows are gone, and before that the forward copy never touched them.
"""
import django.db.models.deletion
from django.db import migrations, models


def copy_slugs(apps, schema_editor):
    Shareout = apps.get_model("shareouts", "Shareout")
    for pk, slug in Shareout.objects.exclude(project=None).values_list("pk", "project__slug"):
        Shareout.objects.filter(pk=pk).update(project_slug=slug)


class Migration(migrations.Migration):

    dependencies = [
        ("shareouts", "0007_shareout_created_by"),
        ("projects", "0012_delete_insight_rows"),
    ]

    operations = [
        migrations.AddField(
            model_name="shareout",
            name="project_slug",
            field=models.CharField(
                blank=True, db_index=True, help_text="Null = cross-project roll-up for the period.",
                max_length=200, null=True,
            ),
        ),
        migrations.RunPython(copy_slugs, migrations.RunPython.noop),
        migrations.RemoveField(model_name="shareout", name="project"),
        migrations.AlterField(
            model_name="shareout",
            name="workspace",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "The tenant that owns this shareout. Shareout is its own tenant root "
                    "(project_slug is orthogonal). Nullable for migration safety; the "
                    "API always assigns one (default workspace when unspecified)."
                ),
                null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="shareouts", to="workspaces.workspace",
            ),
        ),
    ]
