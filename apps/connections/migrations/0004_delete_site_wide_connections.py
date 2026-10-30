"""Delete every Connection (and its grants) before connections move to per-workspace apps.

Each existing row was minted by the site-wide OAuth client that 0005 removes
(`SALESFORCE_CLIENT_ID`/`SECRET` in settings). Without that client a row can
neither refresh nor be revoked, and no workspace app minted it, so it cannot be
given the NOT NULL `Connection.app` 0005 adds. None exist in production; any
elsewhere must be connected again once the workspace's app is set up.

Rolling back does not bring them back (the reverse is a no-op): a rollback past
here loses every connection.

Its own migration, ahead of 0005's schema change to the same tables, because
deleting rows other tables reference (ConnectionGrant -> Connection) in the same
transaction as an ALTER fails on PostgreSQL ("pending trigger events").
"""
from django.db import migrations


def delete_connections(apps, schema_editor):
    apps.get_model("connections", "ConnectionGrant").objects.all().delete()
    apps.get_model("connections", "Connection").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("connections", "0003_access_token_seconds"),
    ]

    operations = [
        migrations.RunPython(delete_connections, migrations.RunPython.noop),
    ]
