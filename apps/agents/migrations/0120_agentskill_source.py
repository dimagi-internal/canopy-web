from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("agents", "0119_use_ask_lines_become_read_only")]

    operations = [
        migrations.AddField(
            model_name="agentskill",
            name="source",
            field=models.CharField(default="published", max_length=16),
        ),
    ]
