import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0073_runnermint_box_slot"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="runnermint",
            name="login_key",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.CreateModel(
            name="RunnerLogin",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=32)),
                ("position", models.PositiveIntegerField(default=0)),
                ("account", models.CharField(blank=True, default="", max_length=254)),
                ("signed_in", models.BooleanField(default=False)),
                ("reported_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("runner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="logins", to="harness.runner")),
            ],
            options={"ordering": ["position", "created_at"]},
        ),
        migrations.AddConstraint(
            model_name="runnerlogin",
            constraint=models.UniqueConstraint(fields=("runner", "key"), name="uniq_runner_login_key"),
        ),
    ]
