from django.db import migrations


class Migration(migrations.Migration):
    """A zero-knowledge agent's read always needs a device proof — a login token alone
    opens nothing — so the switch that asked for one added nothing. An agent that holds
    no value has nothing behind a proof."""

    dependencies = [
        ("agents", "0107_remove_1password"),
    ]

    operations = [
        migrations.RemoveField(model_name="agent", name="require_device_binding"),
    ]
