"""Routing rules no longer carry a mode: 0145 copied each into `TurnModeRule`."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("harness", "0145_turn_mode_rule"),
    ]

    operations = [
        migrations.RemoveField(model_name="runnerassignment", name="turn_mode"),
    ]
