from django.db import migrations


class Migration(migrations.Migration):
    """A GitHub token a person lends an agent lives in the agent's vault, sealed to the machines
    that run it; canopy-web cannot read it. The older table held the token encrypted under
    canopy-web's own key (`secret_enc`), so a copy canopy-web COULD read. Any row still in it is
    deleted with the table: its person lends the token again (Settings → External accounts →
    GitHub), and the new copy is the sealed one."""

    dependencies = [
        ("agents", "0115_vault_attachment_project"),
    ]

    operations = [
        migrations.DeleteModel(name="AgentDelegation"),
    ]
