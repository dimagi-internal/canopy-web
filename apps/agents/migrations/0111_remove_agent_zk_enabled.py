from django.db import migrations


class Migration(migrations.Migration):
    """Every agent is zero-knowledge, so the flag that said so (False only for an agent
    that had never held a value) is gone. `zk_kv` stays: 0 means no vault key yet."""

    dependencies = [
        ("agents", "0110_access_list_contact_and_flag"),
    ]

    operations = [
        migrations.RemoveField(model_name="agent", name="zk_enabled"),
    ]
