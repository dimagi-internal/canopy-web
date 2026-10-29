from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tokens", "0040_site_sign_offs"),
    ]

    operations = [
        migrations.AddField(
            model_name="hostgrant",
            name="requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
