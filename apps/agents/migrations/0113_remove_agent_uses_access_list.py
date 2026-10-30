from django.db import migrations


class Migration(migrations.Migration):
    """Every agent runs on its access list (0112), so there is nothing left to switch."""

    dependencies = [("agents", "0112_move_access_rules_onto_lists")]
    operations = [migrations.RemoveField(model_name="agent", name="uses_access_list")]
