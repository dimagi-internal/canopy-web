"""Adopt `GitHubConnection` from apps/tokens with its existing table — state only."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("tokens", "0034_githubconnection_moved_out"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="GitHubConnection",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                                   serialize=False, verbose_name="ID")),
                        ("github_login", models.CharField(max_length=100)),
                        ("github_user_id", models.BigIntegerField()),
                        ("refresh_token_enc", models.TextField(blank=True, default="")),
                        ("refresh_token_expires_at", models.DateTimeField(blank=True, null=True)),
                        ("access_token_enc", models.TextField(blank=True, default="")),
                        ("access_token_expires_at", models.DateTimeField(blank=True, null=True)),
                        ("refresh_failed_at", models.DateTimeField(blank=True, null=True)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                        ("last_used_at", models.DateTimeField(blank=True, null=True)),
                        ("user", models.OneToOneField(
                            on_delete=django.db.models.deletion.CASCADE,
                            related_name="github_connection", to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        "verbose_name": "GitHub connection",
                        "db_table": "tokens_githubconnection",
                    },
                ),
            ],
            database_operations=[],
        ),
    ]
