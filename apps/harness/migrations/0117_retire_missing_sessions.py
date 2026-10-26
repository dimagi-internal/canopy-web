# The "missing sessions" check is retired: canopy never asks a box for sessions it
# was not sent, so a box is never asked for a full rescan.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0116_credential_set_vault_per_tree"),
    ]

    operations = [
        migrations.RemoveField(model_name="runner", name="usage_scan_requested_at"),
        migrations.RemoveField(model_name="runner", name="usage_scanned_at"),
    ]
