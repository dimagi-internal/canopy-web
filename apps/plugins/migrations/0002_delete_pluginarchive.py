# The archive path is retired: each catalog entry is now a `github` source the
# user's own git fetches, so canopy no longer builds or stores plugin zips
# (docs/architecture/plugin-marketplace.md).

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("plugins", "0001_initial"),
    ]

    operations = [
        migrations.DeleteModel(name="PluginArchive"),
    ]
