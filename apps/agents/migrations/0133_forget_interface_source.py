"""`Agent.interface_source` leaves the code; on PostgreSQL its column stays one more deploy.

Nothing has read the column since the interface routes were retired, and every save wrote "". The
code stops naming it here, so a task on this image neither reads nor writes it. On PostgreSQL the
column is dropped by `0134`, in a later deploy, so that no task still running the previous image
(which selects it) meets a table without it. Until then a new row needs the column filled without the
code naming it, so it gets a database default of "" (it is NOT NULL). That statement changes only the
catalog. Any other database (SQLite in tests) has no running tasks to protect and drops it now.
"""
from django.db import migrations


def _forwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("ALTER TABLE agents_agent ALTER COLUMN interface_source SET DEFAULT ''")
        return
    Agent = apps.get_model("agents", "Agent")
    schema_editor.remove_field(Agent, Agent._meta.get_field("interface_source"))


def _backwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("ALTER TABLE agents_agent ALTER COLUMN interface_source DROP DEFAULT")
        return
    Agent = apps.get_model("agents", "Agent")
    schema_editor.add_field(Agent, Agent._meta.get_field("interface_source"))


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0132_profiles_lose_pages"),
        # Its RunPython reads `interface_source` through the historical model, so it must run first.
        ("harness", "0108_sensitive_data_vocabulary"),
    ]
    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(_forwards, _backwards)],
            state_operations=[migrations.RemoveField(model_name="agent", name="interface_source")],
        ),
    ]
