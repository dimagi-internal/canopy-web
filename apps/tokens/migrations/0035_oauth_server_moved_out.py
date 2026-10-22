"""The MCP sign-in's models moved to apps/oauth_server (tables kept) — state only."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("oauth_server", "0001_initial"),
        ("tokens", "0034_githubconnection_moved_out"),
    ]

    state_operations = [
        migrations.RemoveField(
            model_name="oauthgrant",
            name="client",
        ),
        migrations.RemoveField(
            model_name="oauthgrant",
            name="user",
        ),
        migrations.AlterField(
            model_name="personaltoken",
            name="oauth_grant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="access_tokens",
                to="oauth_server.oauthgrant",
            ),
        ),
        migrations.DeleteModel(
            name="OAuthAuthorizationCode",
        ),
        migrations.DeleteModel(
            name="OAuthClient",
        ),
        migrations.DeleteModel(
            name="OAuthGrant",
        ),
    ]

    # The tables are where they were (each model keeps its `db_table`), so
    # only Django's model state changes.
    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=state_operations, database_operations=[]),
    ]
