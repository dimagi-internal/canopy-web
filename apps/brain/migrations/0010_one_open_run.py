from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("brain", "0009_close_duplicate_open_runs"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="brainrun",
            constraint=models.UniqueConstraint(condition=models.Q(("state", "open")),
                                               fields=("capability", "subject_type", "subject"),
                                               name="brain_one_open_run"),
        ),
    ]
