from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("slack", "0024_slack_choice_independent_buttons"),
    ]

    operations = [
        migrations.RemoveField(model_name="slackchoicepost", name="chosen_at"),
        migrations.RemoveField(model_name="slackchoicepost", name="chosen_index"),
        migrations.RemoveField(model_name="slackchoicepost", name="chosen_by"),
    ]
