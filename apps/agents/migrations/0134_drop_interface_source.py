"""Drop the column `agents_agent.interface_source`.

`0133` took it out of the code and gave it a database default; this runs only once an image with `0133`
has deployed, so no running task still selects it. On any other database `0133` already dropped it.
A metadata-only change on PostgreSQL: it takes a brief lock and rewrites nothing.
"""
from django.db import migrations


def _forwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("ALTER TABLE agents_agent DROP COLUMN IF EXISTS interface_source")


def _backwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("ALTER TABLE agents_agent ADD COLUMN IF NOT EXISTS interface_source text NOT NULL "
                              "DEFAULT ''")


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0133_forget_interface_source"),
    ]
    operations = [migrations.RunPython(_forwards, _backwards)]
