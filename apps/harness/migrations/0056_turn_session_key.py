"""Turn.emdash_task_id -> Turn.session_key.

A RENAME, written by hand: the autodetector (non-interactive) proposed
RemoveField + AddField, which would have dropped every turn's close-out join key.

The column held an emdash task name until the cloud runner started recording
agent sessions (#1057) and writing Claude session ids into it — it is the turn's
session key, the same value RunnerBinding.session_key holds. The wire keeps
accepting the old spelling (apps/harness/schemas.py, apps/agents/schemas.py).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0055_runnerflag"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="turn",
            name="harness_tur_agent_i_984782_idx",
        ),
        migrations.RenameField(
            model_name="turn",
            old_name="emdash_task_id",
            new_name="session_key",
        ),
        migrations.AlterField(
            model_name="turn",
            name="session_key",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "The session this turn drove (emdash task or Claude session id) "
                    "— the close-out join key."
                ),
                max_length=200,
            ),
        ),
        migrations.AddIndex(
            model_name="turn",
            index=models.Index(fields=["agent", "session_key"], name="harness_tur_agent_i_b05434_idx"),
        ),
    ]
