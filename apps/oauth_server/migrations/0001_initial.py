"""Adopt the MCP sign-in's models from apps/tokens, keeping their tables — state only."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("tokens", "0034_githubconnection_moved_out"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    state_operations = [
        migrations.CreateModel(
            name="OAuthClient",
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
                ("client_id", models.CharField(max_length=64, unique=True)),
                ("client_name", models.CharField(blank=True, max_length=200)),
                ("redirect_uris", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "oauth_clients",
            },
        ),
        migrations.CreateModel(
            name="OAuthGrant",
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
                ("scope", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "refresh_token_hash",
                    models.CharField(blank=True, max_length=64, null=True, unique=True),
                ),
                (
                    "previous_refresh_hash",
                    models.CharField(
                        blank=True, db_index=True, max_length=64, null=True
                    ),
                ),
                ("refresh_expires_at", models.DateTimeField(blank=True, null=True)),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="grants",
                        to="oauth_server.oauthclient",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="oauth_grants",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "oauth_grants",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="OAuthAuthorizationCode",
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
                ("code_hash", models.CharField(max_length=64, unique=True)),
                ("redirect_uri", models.TextField()),
                ("code_challenge", models.CharField(max_length=128)),
                ("scope", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="codes",
                        to="oauth_server.oauthclient",
                    ),
                ),
                (
                    "grant",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="oauth_server.oauthgrant",
                    ),
                ),
            ],
            options={
                "db_table": "oauth_authorization_codes",
            },
        ),
    ]

    # The tables are where they were (each model keeps its `db_table`), so
    # only Django's model state changes.
    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=state_operations, database_operations=[]),
    ]
