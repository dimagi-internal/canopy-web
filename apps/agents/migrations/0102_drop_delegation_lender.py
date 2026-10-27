from django.db import migrations


class Migration(migrations.Migration):
    """Salesforce lending is gone: nothing reads `AgentDelegation.lender`."""

    dependencies = [
        ("agents", "0101_drop_custodial_agent_values"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="agentdelegation",
            name="lender",
        ),
    ]
