from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tokens", "0041_host_grant_requested_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="hostgrant",
            name="requested_reason",
            field=models.CharField(blank=True, default="", max_length=300),
        ),
    ]
