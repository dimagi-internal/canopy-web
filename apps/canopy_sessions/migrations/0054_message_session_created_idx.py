"""Index each session's messages by time, for `transcript_read.last_message_at`.

Built CONCURRENTLY on Postgres: the message table is ~526k rows on the shared
labs RDS and is written on every transcript ingest, so a plain CREATE INDEX
would hold those writes for the length of the build. The sqlite test database
has no CONCURRENTLY, so there it is an ordinary AddIndex.
"""

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
        ("canopy_sessions", "0053_session_handoff"),
    ]

    operations = [
        _AddIndexConcurrentlyOnPostgres(
            model_name="message",
            index=models.Index(fields=["session", "-created_at"], name="message_session_created_idx"),
        ),
    ]
