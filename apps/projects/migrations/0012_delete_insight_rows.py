"""Delete the retired Insights feed's rows and drop the `insight` choice.

The feed was removed in #1182 (2026-10); its 45 remaining cards were stale
pre-agentic portfolio notes. Jonathan approved deleting them (2026-10-06).
Irreversible by design: the reverse is a no-op, the rows do not come back.
"""

from django.db import migrations, models


def delete_insight_rows(apps, schema_editor):
    ProjectContext = apps.get_model("projects", "ProjectContext")
    ProjectContext.objects.filter(context_type="insight").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0011_default_identity_agent_hal"),
    ]

    operations = [
        migrations.RunPython(delete_insight_rows, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="projectcontext",
            name="context_type",
            field=models.CharField(
                choices=[
                    ("current_work", "Current Work"),
                    ("next_step", "Next Step"),
                    ("summary", "Summary"),
                    ("note", "Note"),
                ],
                max_length=20,
            ),
        ),
    ]
