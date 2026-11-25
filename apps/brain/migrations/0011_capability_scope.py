from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("brain", "0010_one_open_run"),
    ]

    operations = [
        migrations.AddField(
            model_name="braincapability",
            name="scope",
            field=models.CharField(db_default="full", default="full", max_length=16),
        ),
    ]
