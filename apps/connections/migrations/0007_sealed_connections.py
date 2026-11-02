"""Sealed connection rows: `Connection` and `ConnectionGrant` are dropped and made
again (0006 emptied both), and `ConnectionApp` gains `removed_at`.

The new `Connection` has a UUID primary key (its binding id is `connection:<uuid>`)
and no token column: the refresh token is the `sealed` envelope, the access token
the `access_sealed` one, and canopy-web can open neither. Recreated rather than
altered: the primary key changes type and every token column goes, and both
tables are empty.

Rolling back loses nothing more than 0006 already did: the reverse recreates
the old, empty tables.
"""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0105_agentgooglemint_session_minutes"),
        ("connections", "0006_delete_connections"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.DeleteModel(name="ConnectionGrant"),
        migrations.DeleteModel(name="Connection"),
        migrations.AddField(
            model_name="connectionapp",
            name="removed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="Connection",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("provider", models.CharField(max_length=40)),
                ("account_id", models.CharField(max_length=255)),
                ("account_label", models.CharField(blank=True, default="", max_length=255)),
                ("instance_url", models.URLField(blank=True, default="")),
                ("scopes", models.CharField(blank=True, default="", max_length=500)),
                (
                    "state",
                    models.CharField(
                        choices=[("unshared", "Not shared yet"), ("active", "Active"), ("revoking", "Revoking")],
                        default="unshared",
                        max_length=10,
                    ),
                ),
                ("sealed", models.TextField(blank=True, default="")),
                ("sealed_sig", models.JSONField(blank=True, null=True)),
                ("sealed_digest", models.CharField(blank=True, default="", max_length=64)),
                ("access_sealed", models.TextField(blank=True, default="")),
                ("access_sig", models.JSONField(blank=True, null=True)),
                ("access_expires_at", models.DateTimeField(blank=True, null=True)),
                ("owner_key", models.CharField(blank=True, default="", max_length=64)),
                ("statement", models.JSONField(blank=True, null=True)),
                ("statement_sig", models.CharField(blank=True, default="", max_length=128)),
                ("statement_version", models.PositiveIntegerField(default=0)),
                ("lease_id", models.UUIDField(blank=True, null=True)),
                ("lease_holder", models.CharField(blank=True, default="", max_length=64)),
                ("lease_expires_at", models.DateTimeField(blank=True, null=True)),
                ("lease_written", models.BooleanField(default=False)),
                ("refresh_failed_at", models.DateTimeField(blank=True, null=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("revoking_since", models.DateTimeField(blank=True, null=True)),
                ("revoke_at_provider", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "agent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="connections",
                        to="agents.agent",
                    ),
                ),
                (
                    "app",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="connections",
                        to="connections.connectionapp",
                    ),
                ),
                (
                    "connected_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "signer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="connections",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("agent__isnull", True), ("user__isnull", False)),
                            models.Q(("agent__isnull", False), ("user__isnull", True)),
                            _connector="OR",
                        ),
                        name="connection_exactly_one_owner",
                    ),
                    models.UniqueConstraint(
                        fields=("user", "app", "account_id"), name="connection_unique_user_app_account"
                    ),
                    models.UniqueConstraint(
                        fields=("agent", "app", "account_id"), name="connection_unique_agent_app_account"
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ConnectionGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "agent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="connection_grants",
                        to="agents.agent",
                    ),
                ),
                (
                    "connection",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="grants",
                        to="connections.connection",
                    ),
                ),
                (
                    "granted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("connection", "agent"), name="connectiongrant_unique"),
                ],
            },
        ),
    ]
