"""Runner.paired_by -> Runner.owner.

The field always meant the runner's owner — the human whose token the box
authenticates with, whose memberships it claims under, and who alone may grant
administration. "Paired by" named how ownership was acquired, not what it is.
Column rename only; no data changes. (The AlterField is the help_text wording.)
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("harness", "0058_merge_orphan_closeouts"),
    ]

    operations = [
        migrations.RenameField(model_name="runner", old_name="paired_by", new_name="owner"),
        migrations.AlterField(
            model_name="runner",
            name="workspace",
            field=models.ForeignKey(
                blank=True,
                help_text="The tenant that owns this runner. Nullable for migration safety; the API "
                "assigns one at pairing (the owner's default workspace when unspecified). Mirrors "
                "Agent.workspace.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="runners",
                to="workspaces.workspace",
            ),
        ),
    ]
