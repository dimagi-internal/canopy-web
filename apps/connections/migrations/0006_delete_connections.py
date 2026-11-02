"""Delete every Connection (and its grants) before the token columns go.

Each row holds a refresh token canopy-web could decrypt; the rows that replace
them are sealed to their owner at connect time, and an old token cannot be
sealed without the owner's key. So nothing is carried over: every account is
connected again (spec §9, "Moving over").

Rolling back does not bring them back (the reverse is a no-op).

Its own migration, ahead of 0007's schema change to the same tables, because
deleting rows other tables reference (ConnectionGrant -> Connection) in the same
transaction as an ALTER fails on PostgreSQL ("pending trigger events").
"""
from django.db import migrations


def delete_connections(apps, schema_editor):
    apps.get_model("connections", "ConnectionGrant").objects.all().delete()
    apps.get_model("connections", "Connection").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("connections", "0005_connection_apps"),
    ]

    operations = [
        migrations.RunPython(delete_connections, migrations.RunPython.noop),
    ]
