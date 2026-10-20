from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mail", "0005_verification_error"),
    ]

    operations = [
        migrations.AddField(
            model_name="maildomain",
            name="agent_addresses",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="mailroute",
            name="auto",
            field=models.BooleanField(default=False),
        ),
    ]
