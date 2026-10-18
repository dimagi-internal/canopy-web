from django.db import migrations, models


class Migration(migrations.Migration):
    """A no-turn read labelled as a verification check gets its own alert kind
    (choices only; no schema change)."""

    dependencies = [("agents", "0059_zk_for_agents_holding_nothing")]

    operations = [
        migrations.AlterField(
            model_name="agentcredentialalert",
            name="kind",
            field=models.CharField(
                choices=[
                    ("no-turn", "Read outside a turn, not by an owner or admin"),
                    ("no-turn-check", "Read outside a turn, labelled as a verification check"),
                    ("undeclared-ref", "Read of a ref the agent does not declare"),
                    ("burst", "Burst of reads"),
                    ("device-proof", "Resolve refused: bad or missing device proof"),
                    ("zk-wrap-missing", "Resolve refused: the device holds no vault-key wrap"),
                ],
                max_length=20,
            ),
        ),
    ]
