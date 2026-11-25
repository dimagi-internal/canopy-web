"""A source is due from when it is made (a default, no SQL), and three indexes: open sources by when they are due
(`associate.run_due`), runs by feature and subject (`reading.learned`) and by target and day (`budget`).

Built CONCURRENTLY on Postgres, so the build never holds the writers of these tables; the sqlite test database has no
CONCURRENTLY, so there each is an ordinary AddIndex (as `canopy_sessions` 0054)."""

import django.utils.timezone
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class _AddIndexConcurrentlyOnPostgres(AddIndexConcurrently):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            return super().database_forwards(app_label, schema_editor, from_state, to_state)
        return migrations.AddIndex.database_forwards(self, app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            return super().database_backwards(app_label, schema_editor, from_state, to_state)
        return migrations.AddIndex.database_backwards(self, app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("brain", "0023_source_due_when_made"),
    ]

    operations = [
        migrations.AlterField(
            model_name="brainsource",
            name="associate_due_at",
            field=models.DateTimeField(blank=True, default=django.utils.timezone.now, null=True),
        ),
        _AddIndexConcurrentlyOnPostgres(
            model_name="brainsource",
            index=models.Index(condition=models.Q(("state", "open")), fields=["associate_due_at"],
                               name="brain_source_due"),
        ),
        _AddIndexConcurrentlyOnPostgres(
            model_name="brainrun",
            index=models.Index(fields=["capability", "subject_type", "subject", "state"], name="brain_run_by_subject"),
        ),
        _AddIndexConcurrentlyOnPostgres(
            model_name="brainrun",
            index=models.Index(fields=["target", "created_at"], name="brain_run_by_target_day"),
        ),
    ]
