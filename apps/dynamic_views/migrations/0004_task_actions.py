from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dynamic_views", "0003_connections"),
    ]

    operations = [
        migrations.AddField(
            model_name="dynamicview",
            name="task_actions",
            field=models.JSONField(blank=True, db_default=[], default=list),
        ),
        migrations.AddField(
            model_name="dynamicviewrevision",
            name="task_actions",
            field=models.JSONField(blank=True, db_default=[], default=list),
        ),
    ]
