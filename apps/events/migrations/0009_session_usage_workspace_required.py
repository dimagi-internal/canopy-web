# Every usage row is a session canopy has a record of, so it always has a workspace.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0008_drop_missing_sessions"),
        ("workspaces", "0013_system_accounts"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sessionusage",
            name="workspace",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="session_usage",
                to="workspaces.workspace"),
        ),
    ]
