from django.db import migrations, models


class Migration(migrations.Migration):
    """When a runner last asked for work, apart from when it last heartbeated."""

    dependencies = [
        ("harness", "0131_mail_thread_coalescing"),
    ]

    operations = [
        migrations.AddField(
            model_name="runner",
            name="last_claim_poll_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
