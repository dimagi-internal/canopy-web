"""Per-workspace OAuth apps: `ConnectionApp`, and every `Connection` points at the app that minted it.

`Connection.app` is NOT NULL with no default: 0004 emptied the table first.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0106_credential_alert_context"),
        ("connections", "0004_delete_site_wide_connections"),
        ("workspaces", "0016_retire_system_accounts"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ConnectionApp",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("provider", models.CharField(max_length=40)),
                ("client_id", models.CharField(max_length=255)),
                ("client_secret_enc", models.TextField(blank=True, default="")),
                ("options", models.JSONField(blank=True, default=dict)),
                ("scopes", models.CharField(blank=True, default="", max_length=1000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.RemoveConstraint(
            model_name="connection",
            name="connection_unique_user_account",
        ),
        migrations.RemoveConstraint(
            model_name="connection",
            name="connection_unique_agent_account",
        ),
        migrations.AddField(
            model_name="connectionapp",
            name="configured_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="connectionapp",
            name="workspace",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="connection_apps",
                to="workspaces.workspace",
            ),
        ),
        migrations.AddField(
            model_name="connection",
            name="app",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="connections",
                to="connections.connectionapp",
            ),
        ),
        migrations.AddConstraint(
            model_name="connection",
            constraint=models.UniqueConstraint(
                fields=("user", "app", "account_id"),
                name="connection_unique_user_app_account",
            ),
        ),
        migrations.AddConstraint(
            model_name="connection",
            constraint=models.UniqueConstraint(
                fields=("agent", "app", "account_id"),
                name="connection_unique_agent_app_account",
            ),
        ),
        migrations.AddConstraint(
            model_name="connectionapp",
            constraint=models.UniqueConstraint(
                fields=("workspace", "provider"),
                name="connectionapp_unique_workspace_provider",
            ),
        ),
    ]
