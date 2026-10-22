"""`GitHubConnection` moved to apps/github_connect, keeping its table.

State only: the table `tokens_githubconnection` and its rows stay exactly where
they are; github_connect 0001 adopts it with `db_table` set.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("tokens", "0033_bound_agent"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.DeleteModel(name="GitHubConnection")],
            database_operations=[],
        ),
    ]
