"""Collapse the pre-lifecycle Sessions backlog.

Until now nothing could ever retire a session row, so labs accumulated one per emdash
task any runner ever reported — most of them tasks that no longer exist. Apply the new
staleness rule once so the list starts clean.

Irreversible by design, and safe to be: un-archiving happens naturally on the next
report, so the reverse is a genuine no-op rather than lost information.
"""
from django.db import migrations

from apps.canopy_sessions.staleness import archive_stale_sessions


def forwards(apps, schema_editor):
    archive_stale_sessions(apps.get_model("canopy_sessions", "Session"))


class Migration(migrations.Migration):

    dependencies = [
        ("canopy_sessions", "0008_runnerbinding_backfill_requested"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
