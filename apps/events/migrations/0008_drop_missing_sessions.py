# The "missing sessions" list is retired: canopy keeps the usage of the sessions it
# was sent and nothing else. Deletes the usage rows of sessions canopy had no record
# of (workspace NULL: their repo, first-prompt title and token counts) and the
# `title` column, which only that list showed.

from django.db import migrations


def drop_missing(apps, schema_editor):
    apps.get_model("events", "SessionUsage").objects.filter(workspace__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0007_turn_facts_name_their_thread"),
    ]

    operations = [
        migrations.RunPython(drop_missing, migrations.RunPython.noop),
        migrations.RemoveField(model_name="sessionusage", name="title"),
    ]
