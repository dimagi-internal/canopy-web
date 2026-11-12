from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tokens", "0044_delegated_token_own_panel"),
    ]

    operations = [
        migrations.AddField(
            model_name="delegatedtoken",
            name="page_app",
            field=models.CharField(blank=True, default="", max_length=150),
        ),
    ]
