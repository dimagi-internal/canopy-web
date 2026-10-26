from django.db import migrations


class Migration(migrations.Migration):
    # Nothing routed by it: which program runs a turn is `Agent.clis`, and where a
    # laptop opens a session is `Runner.engine`.
    dependencies = [("agents", "0098_cli_axis")]
    operations = [migrations.RemoveField(model_name="agent", name="runtime_engine")]
