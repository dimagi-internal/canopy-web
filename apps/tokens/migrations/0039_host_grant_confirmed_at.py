from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tokens", "0038_host_grant_revoked_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="hostgrant",
            name="confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
