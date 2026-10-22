"""Drop the fixed credential slot columns 0099 stopped naming.

The model has not named them since 0099, so the state has nothing to change;
only the columns go.
"""
from django.db import migrations

SLOTS = {
    "claude_token_enc": "text",
    "claude_token_secondary_enc": "text",
    "claude_api_key_enc": "text",
    "claude_token_label": "varchar(200)",
    "claude_token_secondary_label": "varchar(200)",
}


class Migration(migrations.Migration):
    dependencies = [("harness", "0100_turn_agent_token_session")]

    operations = [
        migrations.RunSQL(
            f'ALTER TABLE "harness_runnercredential" DROP COLUMN "{name}"',
            reverse_sql=f'ALTER TABLE "harness_runnercredential" ADD COLUMN "{name}" {kind} DEFAULT \'\' NOT NULL')
        for name, kind in SLOTS.items()
    ]
