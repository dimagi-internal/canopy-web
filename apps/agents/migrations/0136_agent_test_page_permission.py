from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0135_task_recurrence_and_comments"),
    ]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="test_page_permission",
            field=models.BooleanField(default=False),
        ),
    ]
