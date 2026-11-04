"""Link any role line `0123` did not see: one written by the previous release while it deployed.

The next migration adds the rule "a line names a role exactly when it points at its row", which a
line written between `0123` and the new code would break. This runs the same linking again (it is
idempotent, and a no-op once the new code is the only writer). Its own migration: it updates rows.
"""
import importlib

from django.db import migrations

_link = importlib.import_module("apps.agents.migrations.0123_link_lines_to_roles")


def forwards(apps, schema_editor):
    _link.link_lines(apps)


class Migration(migrations.Migration):
    dependencies = [("agents", "0123_link_lines_to_roles")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
