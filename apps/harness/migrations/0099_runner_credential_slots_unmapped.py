"""The fixed credential slots leave the model; their columns stay one more release.

Migration 0097 copied each runner's slots into its `RunnerLogin` list, and
nothing has read them since. Dropping the columns in the same release would
break the code still serving during a deploy (its model still names them, so
its writes would too). So this release stops naming them and gives each column
a database default, which lets new rows be written without them; 0101 drops them.
"""
from django.db import migrations, models

SLOTS = {
    "claude_token_enc": models.TextField,
    "claude_token_secondary_enc": models.TextField,
    "claude_api_key_enc": models.TextField,
    "claude_token_label": lambda **kw: models.CharField(max_length=200, **kw),
    "claude_token_secondary_label": lambda **kw: models.CharField(max_length=200, **kw),
}


class Migration(migrations.Migration):
    dependencies = [("harness", "0098_drop_phi_commit_pin")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.AlterField("runnercredential", name,
                                      field(blank=True, default="", db_default=""))
                for name, field in SLOTS.items()
            ],
            state_operations=[migrations.RemoveField("runnercredential", name) for name in SLOTS],
        ),
    ]
