from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0104_project_opt_in"),
    ]

    operations = [
        migrations.AddField(
            model_name="runner",
            name="egress_locked",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="runner",
            name="egress_allow",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
